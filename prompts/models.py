from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import get_language
from django.utils.translation import gettext_lazy as _
from parler.models import TranslatableModel, TranslatedFields
from parler.utils.context import switch_language
from taggit.managers import TaggableManager

from catalog.models import Tool
from core.models.editorial import (
    EditorialManager,
    EditorialQuerySet,
    EditorialWorkflowMixin,
)


class PromptQuerySet(EditorialQuerySet):
    def visible_in_language(self, language_code):
        """
        Public prompts that have an actual translation in language_code -
        never a fallback from another language. Unlike active_translations()
        (used by Guide/UseCase), this never lets an EN-only prompt leak onto
        the DE list or vice versa.

        Beta 11.11D1 added the live-snapshot language gate
        (``EditorialQuerySet.live_snapshot_language_q``) that Use Case and
        Comparison already carried since Beta 11.7/11.9. ``translated()``
        alone only asks whether a translation *row* exists, so a prompt
        published in English and given a German translation afterwards
        counted as publicly visible in German and served that
        never-published draft under ``/de/``. D1 makes that gap load-bearing
        rather than cosmetic - it widens :meth:`visible_on_site` to ``draft``,
        so an edited prompt now stays in the public queryset and would carry
        its unreviewed German draft along - so the gate closes here too.
        """
        return (
            self.visible_on_site()
            .filter(self.live_snapshot_language_q(language_code))
            .translated(language_code)
            .language(language_code)
            .distinct()
        )


PromptManager = EditorialManager.from_queryset(PromptQuerySet)

#: Beta 11.11C4E: stable, versioned schema identifier for `Prompt.live_author`.
#: Never derived from package, git or database state - bumped by hand only if
#: the snapshot shape itself changes. See `Prompt._build_live_author_snapshot()`.
PROMPT_AUTHOR_SNAPSHOT_SCHEMA = "prompt-author-v1"


class Prompt(EditorialWorkflowMixin, TranslatableModel):
    LIVE_SNAPSHOT_FIELDS = ("slug", "public_slug", "title", "intro", "body", "outro")
    live_i18n = models.JSONField(default=dict, blank=True)
    #: Beta 11.11C4E: a global (non-language-specific), immutable snapshot of
    #: the public author display name, frozen at the moment of a conscious
    #: publish/republish - never refreshed by a later name, username or author
    #: change on its own. `None` means no publish snapshot has ever been taken
    #: yet; a dict is always a fully-formed `{"schema": ..., "display_name":
    #: ...}` snapshot, including the explicit "no author" case
    #: (`display_name=""`). See `_build_live_author_snapshot()` for the exact
    #: contract and the module docstring of
    #: `prompts/migrations/0009_prompt_live_author_snapshot.py` for how
    #: existing live snapshots are backfilled. Deliberately global rather than
    #: per-language like `live_i18n`: the author identity has no translation.
    live_author = models.JSONField(null=True, blank=True, default=None)
    translations = TranslatedFields(
        title=models.CharField(_("Title"), max_length=200),
        intro=models.TextField(_("Intro"), blank=True),
        body=models.TextField(_("Body"), blank=True),
        outro=models.TextField(_("Outro"), blank=True),
        slug=models.SlugField(_("Slug"), max_length=220, unique=True),
        public_slug=models.SlugField(_("Public slug"), max_length=220, unique=True, null=True, blank=True),
    )
    tools = models.ManyToManyField(Tool, related_name="prompts", blank=True,
                                   help_text=_("Select the AI tool for which this prompt has been optimized."), )
    tags = TaggableManager(blank=True)

    objects = PromptManager()

    class Meta:
        verbose_name = _("Prompt")
        verbose_name_plural = _("Prompts")

    def __str__(self):
        return self.safe_translation_getter("title", any_language=True) or f"Prompt #{self.pk}"

    def _current_values_for(self, lang: str) -> dict:
        """
        Nur als Legacy-Fallback verwendet, wenn kein Snapshot existiert -
        deshalb ausschließlich dieselbe Sprache: has_translation(lang) muss
        zuerst geprüft werden, da safe_translation_getter() sonst über
        Parlers eigenen (PARLER_LANGUAGES-)Fallback stillschweigend eine
        andere Sprache liefern würde.
        """
        if not self.has_translation(lang):
            return {}
        with switch_language(self, lang):
            get = self.safe_translation_getter
            return {
                "slug": get("slug"),
                "public_slug": get("public_slug"),
                "title": get("title"),
                "intro": get("intro"),
                "body": get("body"),
                "outro": get("outro"),
            }

    def get_display_value(self, field: str, language: str | None = None):
        val = self.get_live_value(field, language)
        if val is not None:
            return val
        lang = language or get_language()
        return self._current_values_for(lang).get(field)

    @property
    def display_title(self):
        return self.get_display_value("title")

    @property
    def display_intro(self):
        return self.get_display_value("intro")

    @property
    def display_body(self):
        return self.get_display_value("body")

    @property
    def display_outro(self):
        return self.get_display_value("outro")

    def get_absolute_url(self, language: str | None = None):
        lang = language or get_language()
        live_slug = self.get_live_value("public_slug", lang) or self.get_live_value("slug", lang)
        if live_slug:
            return reverse("prompts:detail", kwargs={"slug": live_slug})

        curr = self._current_values_for(lang)
        slug = curr.get("public_slug") or curr.get("slug")
        if slug:
            return reverse("prompts:detail", kwargs={"slug": slug})

        for lng in getattr(self, "get_available_languages", list)():
            curr = self._current_values_for(lng)
            slug = curr.get("public_slug") or curr.get("slug")
            if slug:
                return reverse("prompts:detail", kwargs={"slug": slug})
        return "#"

    def _build_live_author_snapshot(self) -> dict[str, str]:
        """
        Beta 11.11C4E: the public author display name at *this* moment,
        frozen as a plain dict - never itself saved, never itself a database
        query beyond the ordinary `self.author` FK access, never dependent on
        request state or the active language.

        `self.author` is used directly rather than a fresh aliased query:
        every real caller (the admin publish action's `select_for_update()`
        loop, the editorial view's freshly `get_object_or_404`-loaded object)
        already holds the correct, just-resolved author for this exact
        publish call, with no unsaved author reassignment in between - see
        the C4E closing report for the callers this was verified against.
        """
        author = self.author
        if author is None:
            display_name = ""
        else:
            display_name = author.get_full_name() or author.username or ""
        return {
            "schema": PROMPT_AUTHOR_SNAPSHOT_SCHEMA,
            "display_name": display_name,
        }

    def on_after_publish(self):
        self.is_published = True
        if not self.published_at:
            self.published_at = timezone.now()
        for lang in self.get_available_languages():
            with switch_language(self, lang):
                if self.slug and self.public_slug != self.slug:
                    self.public_slug = self.slug
        self.live_author = self._build_live_author_snapshot()
