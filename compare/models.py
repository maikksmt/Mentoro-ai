from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _, get_language
from parler.models import TranslatableModel, TranslatedFields
from parler.utils.context import switch_language

from catalog.models import Tool
from core.models.editorial import EditorialWorkflowMixin


class Comparison(EditorialWorkflowMixin, TranslatableModel):
    live_i18n = models.JSONField(default=dict, blank=True)
    LIVE_SNAPSHOT_FIELDS = ("slug", "public_slug", "title", "intro", "body")
    translations = TranslatedFields(
        title=models.CharField(_("Title"), max_length=200),
        intro=models.TextField(_("Intro"), blank=True),
        slug=models.SlugField(_("Slug"), max_length=220, unique=True),
        public_slug=models.SlugField(_("Public slug"), max_length=220, unique=True, null=True, blank=True),
    )
    tools = models.ManyToManyField(Tool, related_name="comparisons", blank=True)
    score_breakdown = models.JSONField(default=dict, blank=True)
    winner = models.ForeignKey(
        Tool, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        verbose_name = _("Comparison")
        verbose_name_plural = _("Comparisons")

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
        if self.score_breakdown and not self.tools.exists():
            raise models.ValidationError({
                "tools": _("At least one tool is required if a score breakdown is provided.")
            })
