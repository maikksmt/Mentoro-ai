# prompts/admin.py
from django.conf import settings
from django.contrib import admin
from django.http import Http404, HttpResponseNotAllowed
from django.shortcuts import render
from django.template.response import TemplateResponse
from django.urls import path
from django.utils import translation
from django.utils.formats import date_format
from django.utils.translation import gettext_lazy as _, get_language, get_language_info
from parler.utils.context import switch_language
from reversion.admin import VersionAdmin

from content.templatetags.richtext import richtext
from core.admin import TranslatableTinyMCEMixin, EditorialWorkflowAdminMixin
from core.editorial_preview import (
    apply_editorial_preview_headers,
    has_saved_translation,
    is_supported_preview_language,
)
from core.services import get_live_display_instance, build_field_diffs
from .models import Prompt
from .presentation import build_draft_prompt_context


@admin.register(Prompt)
class PromptAdmin(EditorialWorkflowAdminMixin, TranslatableTinyMCEMixin, VersionAdmin):
    tinymce_fields = ("intro", "body", "outro")
    list_display = (
        "display_title", "pk", "status", "is_published", "author", "reviewed_by",
        "published_fmt", "updated_fmt",
    )
    list_filter = ("status", "author", "reviewed_by")
    search_fields = ("translations__title", "translations__intro", "translations__body", "translations__slug")
    ordering = ("-published_at", "-updated_at")
    date_hierarchy = "published_at"

    readonly_fields = (
        "status",
        "submitted_for_review_at",
        "reviewed_at",
        "reviewed_by",
        "live_i18n",
        "is_published",
        "public_slug",
        "updated_at",
        "last_published_revision_id",
    )

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
                "is_published",
                "published_at",
                "updated_at",
                "submitted_for_review_at",
                "last_published_revision_id",
                "tools",
            ),
        }),
        (_("Routing"), {
            "fields": ("slug", "public_slug"),
        }),
        (_("Content (translated)"), {
            "fields": ("title", "intro", "body", "outro"),
            "description": _("These fields are language-specific. Use the language tabs above."),
        }),
        (_("Internals"), {
            "classes": ("collapse",),
            "fields": ("live_i18n",),
        }),
    )

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))

        can_edit = self.has_change_permission(request, obj)

        if not can_edit:
            fields += ["intro", "body", "outro"]

        return fields

    def intro(self, obj):
        value = obj.safe_translation_getter("intro", any_language=True)
        return richtext(value or "")

    def body(self, obj):
        value = obj.safe_translation_getter("body", any_language=True)
        return richtext(value or "")

    def outro(self, obj):
        value = obj.safe_translation_getter("outro", any_language=True)
        return richtext(value or "")

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
        return obj.safe_translation_getter("title", any_language=True) or f"Prompt #{obj.pk}"

    def get_urls(self):
        base_urls = super().get_urls()
        custom = [
            path("<path:object_id>/diff/", self.admin_site.admin_view(self.diff_view),
                 name="prompts_prompt_diff"),
            # Beta 11.5: saved-draft preview, mirroring guides_guide_draft_preview
            # (Beta 11.4). admin_site.admin_view() supplies both the staff
            # gate and never_cache (cacheable=False is its default), so the
            # response can never enter a shared cache; the object-level role
            # check happens inside the view itself.
            path(
                "<path:object_id>/preview/<str:language_code>/",
                self.admin_site.admin_view(self.draft_preview_view),
                name="prompts_prompt_draft_preview",
            ),
        ]
        return custom + base_urls

    def draft_preview_view(self, request, object_id, language_code, *args, **kwargs):
        """
        Render one saved prompt draft through the real public detail
        template, in one explicitly requested language.

        Read-only by construction: it resolves the object, builds a context
        and renders. Nothing here saves, transitions the FSM, writes a
        revision or touches ``live_i18n``.

        Permission is the existing object-level editorial contract
        (``EditorialWorkflowAdminMixin.has_change_permission``): Editor/Admin/
        superuser for any prompt, Author for their own only. Everything that
        fails - unknown id, unsupported language, missing translation, or a
        prompt the requester may not preview - answers with the same 404, so
        a non-owning author cannot use the endpoint to confirm that a given
        prompt id exists (deliberately not 403, which would leak exactly that).
        """
        if request.method not in ("GET", "HEAD"):
            return HttpResponseNotAllowed(["GET", "HEAD"])

        if not is_supported_preview_language(language_code):
            raise Http404("Unsupported preview language.")

        prompt = self.get_object(request, object_id)
        if prompt is None or not self.has_change_permission(request, prompt):
            raise Http404("Prompt not found.")

        # Fail closed: no fallback language, no any_language=True, and only a
        # genuinely stored translation counts (see has_saved_translation).
        if not has_saved_translation(prompt, language_code):
            raise Http404("Prompt has no saved translation in this language.")

        # The override covers context building *and* rendering, so nav,
        # breadcrumbs and every {% trans %} resolve in the previewed language;
        # it is scoped, so the ambient language is restored afterwards.
        with translation.override(language_code):
            context = build_draft_prompt_context(prompt, language_code)
            response = render(request, "prompts/prompt_detail.html", context)

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
        obj = self.get_object(request, object_id)
        live_keys = set((obj.live_i18n or {}).keys()) if hasattr(obj, "live_i18n") else set()
        obj_langs = set(getattr(obj, "get_available_languages", lambda: [])())  # Parler
        project_langs = {code for code, _ in getattr(settings, "LANGUAGES", [])}
        langs = []
        for code in list(project_langs) + list(obj_langs) + list(live_keys):
            if code and code not in langs:
                langs.append(code)
        if not langs:
            langs = [get_language()]

        comparisons = []
        for lang in langs:
            with switch_language(obj, lang):
                left = {
                    "slug": obj.safe_translation_getter("slug"),
                    "public_slug": obj.safe_translation_getter("public_slug"),
                    "title": obj.safe_translation_getter("title"),
                    "intro": obj.safe_translation_getter("intro"),
                    "body": obj.safe_translation_getter("body"),
                    "outro": obj.safe_translation_getter("outro"),
                }

            live = get_live_display_instance(obj, lang)
            with switch_language(obj, lang):
                right = {
                    "slug": getattr(live, "slug", None),
                    "public_slug": getattr(live, "public_slug", None),
                    "title": getattr(live, "title", None),
                    "intro": getattr(live, "intro", None),
                    "body": getattr(live, "body", None),
                    "outro": getattr(live, "outro", None),
                }

            changes = build_field_diffs(left, right)
            if not changes:
                continue

            info = get_language_info(lang)
            comparisons.append({
                "code": lang,
                "name": info.get("name_local") or info.get("name") or lang,
                "changes": changes,
            })

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "object": obj,
            "comparisons": comparisons,
        }
        return TemplateResponse(request, "admin/prompts/prompt_diff.html", context)
