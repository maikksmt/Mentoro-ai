from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _, get_language
from parler.models import TranslatableModel, TranslatedFields
from parler.utils.context import switch_language

from catalog.models import Tool
from core.models.editorial import EditorialManager, EditorialQuerySet, EditorialWorkflowMixin


class ComparisonQuerySet(EditorialQuerySet):
    def visible_in_language(self, language_code):
        """
        Public comparisons that have an actual translation in language_code -
        never a fallback from another language (mirrors
        Prompt.objects.visible_in_language() from Beta 8.8). Unlike the
        .published manager's active_translations()-based fallback, this
        guarantees every resulting object's detail URL is reachable under
        language_code's strict ComparisonDetailView resolution.

        Status rule intentionally matches the existing .published manager
        (strict published() only, not the broader visible_on_site() that
        Guide/Prompt use) - this only tightens the language filter, it does
        not widen which statuses are publicly visible.
        """
        return self.published().translated(language_code).language(language_code).distinct()


ComparisonManager = EditorialManager.from_queryset(ComparisonQuerySet)


class Comparison(EditorialWorkflowMixin, TranslatableModel):
    live_i18n = models.JSONField(default=dict, blank=True)
    LIVE_SNAPSHOT_FIELDS = ("slug", "public_slug", "title", "intro", "body")
    translations = TranslatedFields(
        title=models.CharField(_("Title"), max_length=200),
        intro=models.TextField(_("Intro"), blank=True, help_text=_("Short introduction shown in lists and above the comparison.")),
        body=models.TextField(_("Body"), blank=True, help_text=_("Optional longer text with context, recommendations or wrap-up.")),
        slug=models.SlugField(_("Slug"), max_length=220, unique=True, help_text=_("Internal slug, used for the default detail URL.")),
        public_slug=models.SlugField(
            _("Public slug"),
            max_length=220,
            unique=True,
            null=True,
            blank=True,
            help_text=_("Optional public slug. If empty, slug will be used on publish.")
        ),
    )
    tools = models.ManyToManyField(Tool, through="ComparisonToolEntry", related_name="comparisons", blank=True)

    objects = ComparisonManager()

    class Meta:
        verbose_name = _("Tool comparison")
        verbose_name_plural = _("Tool comparisons")
        ordering = ("-published_at", "-updated_at")

    def __str__(self):
        return self.safe_translation_getter("title", any_language=True) or f"Comparison #{self.pk}"

    def get_absolute_url(self, language: str | None = None):
        lang = language or get_language()
        with switch_language(self, lang):
            return reverse("compare:detail", kwargs={"slug": self.slug})

    def on_after_publish(self):
        self.is_published = True
        if not self.published_at:
            self.published_at = timezone.now()
        for lang in self.get_available_languages():
            with switch_language(self, lang):
                if self.slug and self.public_slug != self.slug:
                    self.public_slug = self.slug

    def clean(self):
        super().clean()


class ComparisonToolEntry(TranslatableModel):
    """
    Inhalte pro Tool innerhalb eines Vergleichs.
    - Erlaubt pro Tool strukturierte Darstellung (Summary, Vor-/Nachteile, Besonderheiten)
    - Mehrsprachig via Parler, damit DE/EN unterschiedliche Texte haben können.
    """

    comparison = models.ForeignKey(
        Comparison,
        on_delete=models.CASCADE,
        related_name="tool_entries",
    )
    tool = models.ForeignKey(
        Tool,
        on_delete=models.CASCADE,
        related_name="comparison_entries",
    )

    position = models.PositiveIntegerField(
        default=0,
        help_text=_("Order in which tools are displayed in this comparison."),
    )

    translations = TranslatedFields(
        label=models.CharField(
            _("Label / heading"),
            max_length=200,
            blank=True,
            help_text=_("Optional headline for this tool within the comparison.")
        ),
        summary=models.TextField(
            _("Summary"),
            blank=True,
            help_text=_("Free text: main description for this tool in the comparison.")
        ),
        pros=models.TextField(
            _("Strengths / advantages"),
            blank=True,
            help_text=_("Use bullet lists in TinyMCE to highlight strengths.")
        ),
        cons=models.TextField(
            _("Weaknesses / limitations"),
            blank=True,
            help_text=_("Use bullet lists in TinyMCE to highlight weaknesses.")
        ),
        special=models.TextField(
            _("Special features / notes"),
            blank=True,
            help_text=_("Anything unique, edge cases, or important caveats.")
        ),
    )

    class Meta:
        verbose_name = _("Tool comparison entry")
        verbose_name_plural = _("Tool comparison entries")
        ordering = ("position", "pk")
        unique_together = (("comparison", "tool"),)

    def __str__(self) -> str:
        base = self.safe_translation_getter("label") or str(self.tool)
        return f"{base} @ {self.comparison}"
