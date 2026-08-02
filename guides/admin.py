from django.conf import settings
from django.contrib import admin
from django.http import Http404, HttpResponseNotAllowed
from django.shortcuts import render
from django.template.response import TemplateResponse
from django.urls import path
from django.utils import translation
from django.utils.formats import date_format
from django.utils.translation import get_language, get_language_info
from django.utils.translation import gettext_lazy as _
from parler.admin import TranslatableStackedInline
from parler.utils.context import switch_language
from reversion.admin import VersionAdmin

from content.templatetags.richtext import richtext
from core.admin import (
    ChildOfGuideOwnershipMixin,
    EditorialWorkflowAdminMixin,
    TranslatableTinyMCEInlineMixin,
    TranslatableTinyMCEMixin,
)
from core.editorial_preview import (
    apply_editorial_preview_headers,
    has_saved_translation,
    is_supported_preview_language,
)
from core.services import (
    build_field_diffs,
    build_section_diffs,
    get_live_display_instance,
)

from .models import Guide, GuideItem, GuideSection
from .presentation import build_draft_guide_context


class GuideItemInline(TranslatableTinyMCEInlineMixin, TranslatableStackedInline):
    model = GuideItem
    extra = 0
    fields = ("order", "is_published", "kind", "content_type", "object_id", "url", "title", "teaser")
    tinymce_fields = ("teaser",)
    ordering = ("order", "id")
    show_change_link = True


class GuideSectionInline(TranslatableTinyMCEInlineMixin, TranslatableStackedInline):
    model = GuideSection
    extra = 0
    fields = ("order", "title", "body")
    ordering = ("order", "id")
    tinymce_fields = ("body",)
    show_change_link = True

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))

        if obj is not None:
            is_admin = request.user.is_superuser or request.user.groups.filter(
                name__in=["Admin", "Editor"]
            ).exists()
            is_author = getattr(obj, "author_id", None) == request.user.id

            if not (is_admin or is_author):
                fields += ["title", "body", "order"]

        return fields

    def body(self, obj):
        value = obj.safe_translation_getter("body", any_language=True)
        return richtext(value or "")


@admin.register(Guide)
class GuideAdmin(EditorialWorkflowAdminMixin, TranslatableTinyMCEMixin, VersionAdmin):
    tinymce_fields = ("intro", "body")
    list_display = (
        "display_title", "pk", "status", "is_starter", "is_published", "author", "reviewed_by", "published_fmt",
        "updated_fmt")
    list_filter = ("status", "is_starter", "categories", "author", "reviewed_by")
    search_fields = ("translations__title", "translations__intro", "slug")
    ordering = ("-published_at", "-updated_at")
    date_hierarchy = "published_at"
    readonly_fields = (
        "status", "submitted_for_review_at", "reviewed_at", "reviewed_by", "live_i18n", "is_published", "updated_at",
        "public_slug", "last_published_revision_id",)

    fieldsets = (
        (_("Editorial"), {
            "fields": (
                "status",
                "author",
                "reviewed_by",
                "reviewed_at",
                "review_note",
            )
        }),
        (_("Meta"), {
            "fields": (
                "is_starter",
                "is_published",
                "published_at",
                "updated_at",
                "submitted_for_review_at",
                "last_published_revision_id",

            )
        }),
        (_("Relations"), {
            "fields": ("categories", "tools"),
        }),
        (_("Routing"), {
            "fields": ("public_slug", "slug")
        }),
        (_("Content (translated)"), {
            "fields": ("title", "intro", "body"),
            "description": _("These fields are language-specific. Use the language tabs above."),
        }),

    )

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))

        can_edit = self.has_change_permission(request, obj)

        if not can_edit:
            fields += ["intro", "body"]

        return fields

    def intro(self, obj):
        value = obj.safe_translation_getter("intro", any_language=True)
        return richtext(value or "")

    def body(self, obj):
        value = obj.safe_translation_getter("body", any_language=True)
        return richtext(value or "")

    # ------- kleine Helfer für Spalten -------
    def get_prepopulated_fields(self, request, obj=None):
        return {"slug": ("title",)}

    def published_fmt(self, obj):
        if not obj.published_at:
            return "-"
        return date_format(obj.published_at, format="d.m.Y H:i", use_l10n=True)

    def updated_fmt(self, obj):
        if not obj.updated_at:
            return "-"
        return date_format(obj.updated_at, format="d.m.Y H:i", use_l10n=True)

    @admin.display(ordering="translations__title", description=_("Title"))
    def title_col(self, obj):
        return obj.safe_translation_getter("title", any_language=True) or f"Guide #{obj.pk}"

    title_col.short_description = _("Title")
    title_col.admin_order_field = "translations__title"

    def get_urls(self):
        base_urls = super().get_urls()
        custom = [
            path("<path:object_id>/diff/", self.admin_site.admin_view(self.diff_view), name="guides_guide_diff", ),
            # Beta 11.4: saved-draft preview. admin_site.admin_view() supplies
            # both the staff gate and never_cache (cacheable=False is its
            # default), so the response can never enter a shared cache; the
            # object-level role check happens inside the view itself.
            path(
                "<path:object_id>/preview/<str:language_code>/",
                self.admin_site.admin_view(self.draft_preview_view),
                name="guides_guide_draft_preview",
            ),
        ]
        return custom + base_urls

    def draft_preview_view(self, request, object_id, language_code, *args, **kwargs):
        """
        Render one saved guide draft through the real public detail template,
        in one explicitly requested language.

        Read-only by construction: it resolves the object, builds a context
        and renders. Nothing here saves, transitions the FSM, writes a
        revision or touches ``live_i18n``.

        Permission is the existing object-level editorial contract
        (``EditorialWorkflowAdminMixin.has_change_permission``): Editor/Admin/
        superuser for any guide, Author for their own only. Everything that
        fails - unknown id, unsupported language, missing translation, or a
        guide the requester may not preview - answers with the same 404, so a
        non-owning author cannot use the endpoint to confirm that a given
        guide id exists (deliberately not 403, which would leak exactly that).
        """
        if request.method not in ("GET", "HEAD"):
            return HttpResponseNotAllowed(["GET", "HEAD"])

        if not is_supported_preview_language(language_code):
            raise Http404("Unsupported preview language.")

        guide = self.get_object(request, object_id)
        if guide is None or not self.has_change_permission(request, guide):
            raise Http404("Guide not found.")

        # Fail closed: no fallback language, no any_language=True, and only a
        # genuinely stored translation counts (see has_saved_translation).
        if not has_saved_translation(guide, language_code):
            raise Http404("Guide has no saved translation in this language.")

        # The override covers context building *and* rendering, so nav,
        # breadcrumbs and every {% trans %} resolve in the previewed language;
        # it is scoped, so the ambient language is restored afterwards.
        with translation.override(language_code):
            context = build_draft_guide_context(guide, language_code)
            response = render(request, "guides/guide_detail.html", context)

        return apply_editorial_preview_headers(response, language_code)

    def render_change_form(self, request, context, add=False, change=False, form_url="", obj=None):
        """
        Expose the draft-preview link to the change form.

        The language is the tab Parler currently shows (``get_form_language``),
        never an ambient browser language, and the link is only offered when
        that language actually has a stored translation - otherwise it would
        point at a URL that fail-closes with a 404.
        """
        if obj is not None and obj.pk:
            language_code = self.get_form_language(request, obj)
            context["draft_preview_language"] = language_code
            context["show_draft_preview"] = bool(
                self.has_change_permission(request, obj)
                and has_saved_translation(obj, language_code)
            )
        return super().render_change_form(
            request, context, add=add, change=change, form_url=form_url, obj=obj
        )

    def diff_view(self, request, object_id, *args, **kwargs):
        guide = self.get_object(request, object_id)
        live_keys = set((guide.live_i18n or {}).keys()) if hasattr(guide, "live_i18n") else set()
        obj_langs = set(getattr(guide, "get_available_languages", list)())
        project_langs = {code for code, _ in getattr(settings, "LANGUAGES", [])}
        langs = []
        for code in list(project_langs) + list(obj_langs) + list(live_keys):
            if code and code not in langs:
                langs.append(code)
        if not langs:
            langs = [get_language()]

        comparisons = []
        for lang in langs:
            with switch_language(guide, lang):
                left = {
                    "slug": guide.safe_translation_getter("slug"),
                    "public_slug": guide.safe_translation_getter("public_slug"),
                    "title": guide.safe_translation_getter("title"),
                    "intro": guide.safe_translation_getter("intro"),
                    "body": guide.safe_translation_getter("body"),
                }

            live = get_live_display_instance(guide, lang)
            with switch_language(guide, lang):
                right = {
                    "slug": getattr(live, "slug", None),
                    "public_slug": getattr(live, "public_slug", None),
                    "title": getattr(live, "title", None),
                    "intro": getattr(live, "intro", None),
                    "body": getattr(live, "body", None),
                }

            guide_changes = build_field_diffs(left, right)
            section_changes = build_section_diffs(guide, lang)
            if not guide_changes and not section_changes:
                continue

            info = get_language_info(lang)
            comparisons.append({
                "code": lang,
                "name": info.get("name_local") or info.get("name") or lang,
                "changes": guide_changes,
                "section_changes": section_changes,
            })

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "object": guide,
            "comparisons": comparisons,
        }
        return TemplateResponse(request, "admin/guides/guide_diff.html", context)

    inlines = (GuideSectionInline,)


@admin.register(GuideSection)
class GuideSectionAdmin(ChildOfGuideOwnershipMixin, TranslatableTinyMCEMixin):
    tinymce_fields = ("body",)
    list_display = ("title", "pk", "guide__id", "order")
    list_filter = ("guide__id", "guide")
    ordering = ("guide__id", "order")
    search_fields = ("translations__title",)
    readonly_fields = ("live_i18n",)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "guide" and not self._is_editor_user(request.user):
            kwargs["queryset"] = Guide.objects.filter(author=request.user)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def body(self, obj):
        return richtext(obj.safe_translation_getter("body", any_language=True))

    inlines = (GuideItemInline,)
