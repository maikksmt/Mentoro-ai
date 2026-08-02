from django import forms
from django.contrib import admin
from django.utils.text import capfirst
from django.utils.translation import get_language
from django.utils.translation import gettext_lazy as _
from parler.admin import TranslatableTabularInline
from parler.forms import TranslatableModelForm

from core.admin import TranslatableTinyMCEMixin

from .models import AffiliateProgram, Category, PricingTier, Tool

admin.site.site_header = "MentoroAI – Admin"


class ToolAdminForm(TranslatableModelForm):
    language_support_input = forms.CharField(
        label=_("Language support"),
        required=False,
        help_text=_(
            'Comma-separated language codes (ISO-639-1), e.g. en,es,de,fr,pt,it,nl,jp,ko,cn,hi,ar,tr,ru - all other languages needs full name, use "+" for more'),
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "style": "min-width:30rem;",  # Breite nach Geschmack
            }
        ),
    )

    class Meta:
        model = Tool
        exclude = ("language_support",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and self.instance.language_support:
            self.fields["language_support_input"].initial = ", ".join(
                self.instance.language_support
            )

    def clean_language_support_input(self):
        raw = self.cleaned_data.get("language_support_input", "")
        if not raw.strip():
            return []
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        return parts

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.language_support = self.cleaned_data.get("language_support_input", [])
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class PricingTierInlineForm(TranslatableModelForm):
    # Komfort-Textarea statt JSON direkt
    features_input = forms.CharField(
        label=_("Features"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("One feature per line (stored as a list per language)."),
    )

    class Meta:
        model = PricingTier
        # 'features' selbst bearbeiten wir über features_input
        fields = ("name", "price_month", "price_year", "features_input")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # bestehende Features (für aktuelle Sprache) -> Zeilen
        if self.instance and self.instance.pk:
            features = self.instance.safe_translation_getter(
                "features",
                default=[],
                any_language=False,
            )
            if features:
                self.fields["features_input"].initial = "\n".join(features)

        # Name-Feld im Inline schmaler machen
        if "name" in self.fields:
            w = self.fields["name"].widget
            style = w.attrs.get("style", "")
            w.attrs["style"] = (style + "; max-width: 16rem").strip("; ")

    def clean_features_input(self):
        raw = self.cleaned_data.get("features_input", "") or ""
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        return lines

    def save(self, commit=True):
        instance = super().save(commit=False)
        # features pro Sprache speichern
        instance.features = self.cleaned_data.get("features_input", [])
        if commit:
            instance.save()
        return instance


class PricingInline(TranslatableTabularInline):
    model = PricingTier
    form = PricingTierInlineForm
    extra = 1
    fields = ("name", "price_month", "price_year", "features_input")


class AffiliateInline(admin.TabularInline):
    model = AffiliateProgram
    extra = 0
    fields = ("network", "program_url", "commission_type", "commission_value", "cookie_days", "tracking_note")


@admin.register(Tool)
class ToolAdmin(TranslatableTinyMCEMixin):
    form = ToolAdminForm
    tinymce_fields = ("short_description", "long_description")

    # Listenansicht
    list_display = (
        "name",
        "vendor",
        "pricing_model",
        "free_tier",
        "is_featured",
        "rating",
        "published_at",
        "updated_at",
    )
    list_editable = (
        "free_tier",
        "is_featured",
    )
    list_filter = (
        "is_featured",
        "free_tier",
        "pricing_model",
        "categories",
    )
    search_fields = (
        "translations__name",
        "vendor",
        "translations__short_description",
        "translations__long_description",
    )
    readonly_fields = ("created_at", "updated_at")
    autocomplete_fields = ("categories",)

    inlines = (PricingInline, AffiliateInline)

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "name",
                    "slug",
                    "vendor",
                    "website",
                    "logo",
                )
            },
        ),
        (
            _("Content"),
            {
                "fields": (
                    "short_description",
                    "long_description",
                )
            },
        ),
        (
            _("Classification"),
            {
                "fields": (
                    "categories",
                    "language_support_input",
                )
            },
        ),
        (
            _("Pricing & Highlight"),
            {
                "fields": (
                    "pricing_model",
                    "free_tier",
                    "rating",
                    "is_featured",
                )
            },
        ),
        (
            _("Meta"),
            {
                "fields": (
                    "published_at",
                    "created_at",
                    "updated_at",
                )
            },
        ),
    )

    def get_prepopulated_fields(self, request, obj=None):
        return {"slug": ("name",)}

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name == "categories":
            lang = request.GET.get("language") or get_language()
            kwargs["queryset"] = Category.objects.language(lang)
        return super().formfield_for_manytomany(db_field, request, **kwargs)

    # ------------------------------------------------------------------
    # Beta 11.11D4C: never hard-delete a tool a comparison entry still needs
    # ------------------------------------------------------------------

    def tools_blocked_by_comparison_entries(self, objs):
        """
        The tools among ``objs`` that at least one ``ComparisonToolEntry``
        references, resolved in **one** bundled query.

        ``ComparisonToolEntry.tool`` is ``on_delete=CASCADE`` and the entry is
        not a join row: it carries ``position`` plus its own translated
        ``label``/``summary``/``pros``/``cons``/``special`` content in every
        language. Deleting the tool destroys all of that silently - reproduced
        before this guard existed (one tool, one entry, two translations: the
        entry and both translations went to zero while the comparison
        survived).

        Reached through Tool's own reverse accessor (``related_name=
        "comparison_entries"``), so ``catalog`` needs no import from
        ``compare``. An empty selection issues no query at all.
        """
        pks = [obj.pk for obj in objs if getattr(obj, "pk", None) is not None]
        if not pks:
            return []
        return list(
            Tool.objects.filter(pk__in=pks, comparison_entries__isnull=False)
            .distinct()
            .order_by("pk")
        )

    def get_deleted_objects(self, objs, request):
        """
        Adds those tools to Django's own ``protected`` list.

        This single hook covers both deletion paths, because both consult
        ``protected`` *before* deciding to delete:
        ``ModelAdmin._delete_view`` (``if request.POST and not protected``) and
        ``django.contrib.admin.actions.delete_selected``
        (``if request.POST.get("post") and not protected``). Consequences that
        fall out of that, rather than being re-implemented here:

        * a confirmed POST on a protected tool deletes nothing, even when the
          delete button was never rendered and the request was hand-crafted;
        * a bulk selection containing one protected tool is blocked *as a
          whole* - no partial deletion of the unprotected tools alongside it;
        * Django renders its own "Cannot delete" page naming the blocking
          entries, and its success message is never produced.

        Deliberately not a model, queryset or signal guard and no ``on_delete``
        change: a direct ``tool.delete()`` outside the admin still cascades
        exactly as before (see
        ``catalog/tests/test_admin_tool_deletion_guard.py``).
        """
        to_delete, model_count, perms_needed, protected = super().get_deleted_objects(
            objs, request
        )

        blocked = self.tools_blocked_by_comparison_entries(objs)
        if blocked:
            # Read off the same reverse relation the query above uses, so this
            # module still needs no import from ``compare``.
            entry_model = Tool._meta.get_field("comparison_entries").related_model
            entry_label = capfirst(entry_model._meta.verbose_name)
            protected = [
                *protected,
                *(
                    _("%(entry)s: used by tool %(tool)s - remove or reassign the "
                      "comparison entries first")
                    % {"entry": entry_label, "tool": tool}
                    for tool in blocked
                ),
            ]
        return to_delete, model_count, perms_needed, protected


@admin.register(Category)
class CategoryAdmin(TranslatableTinyMCEMixin):
    list_display = ("name", "slug")
    search_fields = ("translations__name", "translations__description")
    fields = ("slug", "name", "description")

    def get_prepopulated_fields(self, request, obj=None):
        return {"slug": ("name",)}


@admin.register(AffiliateProgram)
class AffiliateProgramAdmin(admin.ModelAdmin):
    list_display = ("tool", "network", "program_url", "commission_type", "commission_value")
    search_fields = ("tool__translations__name", "network")


admin.site.register(PricingTier)
