from django.contrib import admin
from django.utils.formats import date_format
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
from parler.admin import TranslatableStackedInline
from reversion.admin import VersionAdmin

from core.admin import TranslatableTinyMCEMixin, EditorialWorkflowAdminMixin, TranslatableTinyMCEInlineMixin
from .models import Comparison, ComparisonToolEntry


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
        return mark_safe(value or "")

    def body(self, obj):
        value = obj.safe_translation_getter("body", any_language=True)
        return mark_safe(value or "")

    def get_prepopulated_fields(self, request, obj=None):
        return {"slug": ("title",)}

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
