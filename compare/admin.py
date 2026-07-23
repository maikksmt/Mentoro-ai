from django.contrib import admin
from django.http import Http404, HttpResponseNotAllowed
from django.shortcuts import render
from django.urls import path, reverse
from django.utils import translation
from django.utils.formats import date_format
from django.utils.translation import gettext as _t, gettext_lazy as _
from parler.admin import TranslatableStackedInline
from reversion.admin import VersionAdmin

from content.templatetags.richtext import richtext
from core.admin import TranslatableTinyMCEMixin, EditorialWorkflowAdminMixin, TranslatableTinyMCEInlineMixin
from core.editorial_preview import (
    apply_editorial_preview_headers,
    build_preview_seo_meta,
    has_saved_translation,
    is_supported_preview_language,
)
from core.seo.utils import seo_text
from core.services import related_comparisons, to_teaser_item
from .models import Comparison, ComparisonToolEntry
from .presentation import draft_tool_entries


def _draft_translation_value(obj, field: str, language_code: str) -> str:
    """Stored translation value for exactly ``language_code``, or ``""``."""
    return (
        obj.safe_translation_getter(field, language_code=language_code, any_language=False)
        or ""
    )


def build_comparison_draft_preview_context(comparison: Comparison, language_code: str) -> dict:
    """
    Full context for rendering a saved comparison draft through the real
    public detail template.

    Lives here rather than in ``compare/presentation.py``, unlike the
    guide/prompt/use-case equivalents: this function needs
    ``core.services.related_comparisons()``/``to_teaser_item()``, but
    ``core/services.py`` itself imports
    ``live_tool_ids_for_comparisons`` from ``compare/presentation.py`` (Beta
    11.9C) - so ``compare/presentation.py`` importing back from
    ``core.services`` would be circular. ``compare/admin.py`` has no such
    constraint and is the only caller, so the assembly lives here instead;
    ``draft_tool_entries()`` (no ``core.services`` dependency at all) stays
    in ``compare/presentation.py``.

    Must be called inside ``translation.override(language_code)`` so that
    ``{% trans %}``, ``reverse()`` and the related-content lookup all
    resolve in the previewed language rather than the editor's ambient one.

    Parent values come from the comparison's own stored translation for
    ``language_code`` only - never ``live_i18n``, never
    ``display_title``/``display_intro``/``display_body`` (those are
    snapshot-first by design, see ``Comparison.get_display_value()``).
    Entries come from ``draft_tool_entries()`` - the currently saved
    ``ComparisonToolEntry`` rows, never ``live_entries``.

    Related content deliberately reuses ``related_comparisons()`` unchanged:
    it ranks the *source* by its own live tool snapshot (Beta 11.9E), so a
    draft tool swap being previewed here has exactly as much effect on the
    related ranking as it already has on the public page - none, until
    republished. Candidates are drawn from ``visible_in_language()`` and
    rendered through the same live-revision-safe teaser values the public
    page uses, so the preview's related block can never surface anyone
    else's draft.

    The SEO object is intentionally minimal (``build_preview_seo_meta()``):
    ``noindex,nofollow``, empty canonical, no alternates, no JSON-LD - the
    preview URL never enters canonical links, hreflang or structured data.
    """
    title = _draft_translation_value(comparison, "title", language_code)
    intro = _draft_translation_value(comparison, "intro", language_code)
    body = _draft_translation_value(comparison, "body", language_code)

    entries = draft_tool_entries(comparison, language_code)
    related = related_comparisons(comparison, limit=3, language_code=language_code)

    return {
        "object": comparison,
        "display_title": title,
        "display_intro": intro,
        "display_body": body,
        "tool_entries": entries,
        "tools_list": [entry.tool for entry in entries],
        "related_comparisons": [
            to_teaser_item(c, "comparison", language_code=language_code) for c in related
        ],
        "crumbs": [
            (_t("Comparisons"), reverse("compare:index")),
            (title, ""),
        ],
        "seo": build_preview_seo_meta(
            title=title,
            description=seo_text(intro or body or title)[:155],
        ),
        "is_preview": True,
        "preview_language": language_code,
    }


class ComparisonToolEntryInline(TranslatableTinyMCEInlineMixin, TranslatableStackedInline):
    """
    Inline zur Pflege der Tool-Inhalte innerhalb eines Vergleichs.
    """
    model = ComparisonToolEntry
    extra = 2
    min_num = 0
    fk_name = "comparison"
    ordering = ("position",)
    tinymce_fields = ("summary", "pros", "cons", "special",)

    fields = (
        "tool",
        "position",
        "label",
        "summary",
        "pros",
        "cons",
        "special",
    )


@admin.register(Comparison)
class ComparisonAdmin(EditorialWorkflowAdminMixin, TranslatableTinyMCEMixin, VersionAdmin):
    """
    Admin für Tool-Vergleiche:
    - Redaktionsworkflow (FSM-Actions) via EditorialWorkflowAdminMixin
    - TinyMCE für Intro + Body via TranslatableTinyMCEMixin
    - Versionierung via django-reversion
    """

    tinymce_fields = ("intro", "body")

    inlines = (ComparisonToolEntryInline,)

    # ----- Listenansicht -----

    list_display = (
        "admin_title",
        "status",
        "is_published",
        "admin_tools_summary",
        "author",
        "admin_updated",
        "admin_published",
    )
    list_filter = (
        "status",
        "is_published",
        "tools",
    )
    search_fields = (
        "translations__title",
        "translations__intro",
        "translations__body",
    )

    # ----- Readonly / Fieldsets -----

    readonly_fields = (
        "status",
        "admin_status_badge",
        "created_at",
        "updated_at",
        "published_at",
        "is_published",
        "public_slug",
        "reviewed_by",
        "reviewed_at",
        "admin_tools_summary",
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
        (_("Content"), {
            "fields": (
                "title",
                "intro",
                "body",
            )
        }),
        (_("Slug & URLs"), {
            "fields": (
                "slug",
                "public_slug",
            )
        }),
        (_("Timestamps"), {
            "fields": (
                "created_at",
                "updated_at",
                "published_at",
            )
        }),
        (_("[Tools] Overview"), {
            "fields": ("admin_tools_summary",),
        }),
    )

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))

        can_edit = self.has_change_permission(request, obj)

        if not can_edit:
            fields += ["intro, body"]

        return fields

    def intro(self, obj):
        value = obj.safe_translation_getter("intro", any_language=True)
        return richtext(value or "")

    def body(self, obj):
        value = obj.safe_translation_getter("body", any_language=True)
        return richtext(value or "")

    def get_prepopulated_fields(self, request, obj=None):
        return {"slug": ("title",)}

    def get_urls(self):
        base_urls = super().get_urls()
        custom = [
            # Beta 11.10: saved-draft preview, mirroring
            # guides_guide_draft_preview (Beta 11.4), prompts_prompt_draft_preview
            # (Beta 11.5) and usecases_usecase_draft_preview (Beta 11.8).
            # admin_site.admin_view() supplies both the staff gate and
            # never_cache (cacheable=False is its default), so the response
            # can never enter a shared cache; the object-level role check
            # happens inside the view itself.
            path(
                "<path:object_id>/preview/<str:language_code>/",
                self.admin_site.admin_view(self.draft_preview_view),
                name="compare_comparison_draft_preview",
            ),
        ]
        return custom + base_urls

    def draft_preview_view(self, request, object_id, language_code, *args, **kwargs):
        """
        Render one saved comparison draft through the real public detail
        template, in one explicitly requested language.

        Read-only by construction: it resolves the object, builds a context
        and renders. Nothing here saves, transitions the FSM, writes a
        revision or touches ``live_i18n``/``live_entries``.

        Permission is the existing object-level editorial contract
        (``EditorialWorkflowAdminMixin.has_change_permission``): Editor/Admin/
        superuser for any comparison, Author for their own only. Everything
        that fails - unknown id, unsupported language, missing translation,
        or a comparison the requester may not preview - answers with the
        same 404, so a non-owning author cannot use the endpoint to confirm
        that a given comparison id exists (deliberately not 403, which would
        leak exactly that).
        """
        if request.method not in ("GET", "HEAD"):
            return HttpResponseNotAllowed(["GET", "HEAD"])

        if not is_supported_preview_language(language_code):
            raise Http404("Unsupported preview language.")

        comparison = self.get_object(request, object_id)
        if comparison is None or not self.has_change_permission(request, comparison):
            raise Http404("Comparison not found.")

        # Fail closed: no fallback language, no any_language=True, and only a
        # genuinely stored translation counts (see has_saved_translation).
        if not has_saved_translation(comparison, language_code):
            raise Http404("Comparison has no saved translation in this language.")

        # The override covers context building *and* rendering, so nav,
        # breadcrumbs and every {% trans %} resolve in the previewed language;
        # it is scoped, so the ambient language is restored afterwards.
        with translation.override(language_code):
            context = build_comparison_draft_preview_context(comparison, language_code)
            response = render(request, "compare/comparison_detail.html", context)

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

    # ----- Helper-Methods für Admin-UI -----

    def admin_title(self, obj: Comparison):
        return obj.safe_translation_getter("title", any_language=True)

    admin_title.short_description = _("Title")

    def admin_tools_summary(self, obj: Comparison):
        tools = obj.tools.all()
        if not tools:
            return _("No tools selected")
        names = ", ".join(sorted(t.name for t in tools[:4]))
        if tools.count() > 4:
            names += f" (+{tools.count() - 4})"
        return names

    admin_tools_summary.short_description = _("Tools in comparison")

    def admin_updated(self, obj: Comparison):
        if not obj.updated_at:
            return "-"
        return date_format(obj.updated_at, "SHORT_DATETIME_FORMAT")

    admin_updated.short_description = _("Updated")

    def admin_published(self, obj: Comparison):
        if not obj.published_at:
            return "-"
        return date_format(obj.published_at, "SHORT_DATETIME_FORMAT")

    admin_published.short_description = _("Published")

    def admin_status_badge(self, obj):
        # Beispiel: einfache Ausgabe des Status (oder HTML-Badge)
        return obj.get_status_display()

    admin_status_badge.short_description = _("Status")
