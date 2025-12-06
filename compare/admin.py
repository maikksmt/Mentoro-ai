from django.contrib import admin
from django.utils.formats import date_format
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
from reversion.admin import VersionAdmin

from core.admin import TranslatableTinyMCEMixin, EditorialWorkflowAdminMixin
from .models import Comparison


@admin.register(Comparison)
class ComparisonAdmin(EditorialWorkflowAdminMixin, TranslatableTinyMCEMixin, VersionAdmin):
    tinymce_fields = ("intro",)

    # Listenansicht
    list_display = (
        "title_col",
        "status",
        "published_fmt",
        "author",
        "reviewed_by",
        "winner",
        "updated_fmt",
        "pk",
    )
    list_display_links = ("title_col",)
    ordering = ("-published_at", "-updated_at")

    # Filter & Suche
    list_filter = ("status", "author", "reviewed_by")
    search_fields = ("translations__title", "translations__intro", "slug")

    # Felder im Formular
    fieldsets = (
        (_("Meta"), {
            "fields": ("status", "published_at", "updated_at", "author", "reviewed_by")
        }),
        (_("Routing"), {
            "fields": ("slug",)
        }),
        (_("Content (translated)"), {
            "fields": ("title", "intro"),
            "description": _(
                "These fields are language-specific. Use the language tabs above."
            ),
        }),
        (_("Relations"), {
            "fields": ("tools", "winner"),
        }),
        (_("Scoring"), {
            "fields": ("score_breakdown",),
        }),
    )

    readonly_fields = (
        "status", "submitted_for_review_at", "reviewed_at", "reviewed_by", "is_published", "last_published_revision_id", "updated_at",)
    filter_horizontal = ("tools",)

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))

        can_edit = self.has_change_permission(request, obj)

        if not can_edit:
            fields += ["intro"]

        return fields

    def intro(self, obj):
        value = obj.safe_translation_getter("intro", any_language=True)
        return mark_safe(value or "")

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
        return obj.safe_translation_getter("title", any_language=True) or f"Comparison #{obj.pk}"

    title_col.short_description = _("Title")
    title_col.admin_order_field = "translations__title"
