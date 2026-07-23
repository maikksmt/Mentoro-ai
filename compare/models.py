from django.db import models
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _, get_language, override
from parler.models import TranslatableModel, TranslatedFields
from parler.utils.context import switch_language

from catalog.models import Tool
from core.models.editorial import EditorialManager, EditorialQuerySet, EditorialWorkflowMixin

#: ComparisonToolEntry translated fields the public detail page renders.
#: Frozen into ``Comparison.live_entries`` on publish - see
#: ``Comparison.build_live_entries()``.
ENTRY_LIVE_FIELDS = ("label", "summary", "pros", "cons", "special")


class ComparisonQuerySet(EditorialQuerySet):
    #: Editing states in which an already published comparison keeps its
    #: public presence, provided a live revision *and* a published entry
    #: snapshot exist.
    #:
    #: Beta 11.9 adds these to the shared
    #: EditorialQuerySet.visible_on_site() set, mirroring the Use-Case
    #: decision from Beta 11.7A: leaving a comparison offline for the
    #: duration of an editorial round was the defect, not the feature.
    #:
    #: STATUS_ARCHIVED is deliberately absent: archiving *is* the deliberate
    #: public withdrawal and outranks any snapshot still on record.
    #: STATUS_DRAFT is absent too - the FSM only reaches it from archived via
    #: restore(), i.e. after a withdrawal.
    LIVE_EDITING_STATUSES = (
        EditorialWorkflowMixin.STATUS_REVIEW,
        EditorialWorkflowMixin.STATUS_APPROVED,
        EditorialWorkflowMixin.STATUS_REWORK,
    )

    def visible_on_site(self):
        """
        Comparison-specific override of the shared editorial rule.

        Two conditions guard the widened branch, and the second one is what
        makes the widening safe at all:

        * ``last_published_revision_id`` - the live-revision marker every
          editorial model already uses; and
        * a non-NULL ``live_entries`` snapshot, i.e. the comparison was
          published at least once *since* Beta 11.9 introduced the entry
          snapshot.

        Without the second condition a legacy comparison (published before
        this slice, therefore ``live_entries IS NULL``) would stay online
        through an editorial round while ``compare/presentation.py`` still
        had to read its tool entries from the live rows - publishing
        unreviewed entry edits. Requiring the snapshot keeps exactly those
        records on the pre-Beta-11.9 behaviour: public while ``published``,
        offline the moment an edit moves them out of it, until the next
        publish writes a real snapshot. See
        ``compare/presentation.py::public_tool_entries()`` for the matching
        runtime states.

        Overridden here rather than in EditorialQuerySet because that class
        backs Guide, Prompt and Use Case as well. Ordering matches the base
        method so callers see no other behavioural difference.
        """
        return self.filter(
            Q(status=EditorialWorkflowMixin.STATUS_PUBLISHED)
            | (
                Q(
                    status__in=self.LIVE_EDITING_STATUSES,
                    last_published_revision_id__isnull=False,
                )
                & Q(live_entries__isnull=False)
            )
        ).order_by("updated_at")

    def visible_in_language(self, language_code):
        """
        Public comparisons that have an actual translation in language_code -
        never a fallback from another language (mirrors
        Prompt.objects.visible_in_language() from Beta 8.8). Unlike the
        .published manager's active_translations()-based fallback, this
        guarantees every resulting object's detail URL is reachable under
        language_code's strict ComparisonDetailView resolution.

        Status rule (Beta 11.9): :meth:`visible_on_site` - published, or one
        of :attr:`LIVE_EDITING_STATUSES` with both a live revision and a
        published entry snapshot. It replaces the stricter ``published()``
        this queryset carried until Beta 11.8, under which the admin's own
        auto-review guard took an edited comparison's entire public page
        offline (reproduced through the real admin POST in
        compare/tests/test_published_edit_visibility.py).

        The snapshot filter makes the language rule fail-closed, which
        ``translated()`` alone is not: it only asks whether a translation
        *row* exists, so a comparison published in English and given a
        German translation afterwards would count as publicly visible in
        German and serve that never-published draft under /de/. Public
        visibility in a language requires a published revision *in that
        language*, matching core/projections.py's three states:

        * ``live_i18n`` has an entry for ``language_code`` - published here.
        * ``live_i18n`` is entirely empty - a record predating the snapshot
          mechanism; the strict ``published()``-era behaviour is kept for it.
        * ``live_i18n`` is non-empty but lacks this language - published in
          other languages only, so there is no public revision here.
          Excluded.
        """
        return (
            self.visible_on_site()
            .filter(
                Q(**{"live_i18n__has_key": language_code})
                | Q(live_i18n={})
                | Q(live_i18n__isnull=True)
            )
            .translated(language_code)
            .language(language_code)
            .distinct()
        )


ComparisonManager = EditorialManager.from_queryset(ComparisonQuerySet)


class Comparison(EditorialWorkflowMixin, TranslatableModel):
    live_i18n = models.JSONField(default=dict, blank=True)
    LIVE_SNAPSHOT_FIELDS = ("slug", "public_slug", "title", "intro", "body")

    #: Ordered, frozen snapshot of the tool entries as they were published.
    #:
    #: Beta 11.9. Unlike ``live_i18n`` (a dict, where ``{}`` means "legacy,
    #: never snapshotted"), this deliberately uses NULL for that state:
    #: a comparison may legitimately be published with *zero* tool entries,
    #: so ``[]`` has to stay distinguishable from "no snapshot yet".
    #:
    #: * ``None``  - legacy record predating this mechanism. The public page
    #:   falls back to the live rows, exactly as it did before Beta 11.9,
    #:   and ``ComparisonQuerySet.visible_on_site()`` keeps it offline once
    #:   it leaves ``published``.
    #: * ``[]``    - published with no entries.
    #: * ``[...]`` - published entries, in published order.
    #:
    #: Element shape (see :meth:`build_live_entries`)::
    #:
    #:     {"tool_id": 12, "position": 10,
    #:      "translations": {"en": {"label": ..., "summary": ...,
    #:                              "pros": ..., "cons": ..., "special": ...}}}
    live_entries = models.JSONField(null=True, blank=True, default=None)
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
        """
        Beta 8.11: previously used switch_language(self, lang) followed by
        plain `self.slug` attribute access - parler.fields' translated-field
        descriptor calls _get_translated_model(use_fallback=True)
        regardless of any_language, so with PARLER_LANGUAGES' fallback="en"
        / hide_untranslated=False (mentoroai/settings/base.py), requesting a
        language with no translation silently substituted the fallback
        language's slug instead of failing - reversed under `lang`'s own
        i18n_patterns prefix, so an EN-only comparison's get_absolute_url(
        language="de") produced e.g. "/de/compare/<en-slug>/", a target that
        404s. Confirmed via reproduction in
        compare/tests/test_url_language_safety.py.

        Fixed by: reading live_i18n[lang] directly (no cross-language
        fallback, unlike get_live_value()), and otherwise requiring
        has_translation(lang) before ever reading the translation - so a
        missing translation returns "#" (matching Guide/Prompt's existing
        convention for the same case) instead of substituting another
        language's slug. reverse() is wrapped in override(lang) so the
        generated URL's prefix always matches the requested language, not
        whatever Django's ambient active language happens to be (needed for
        the language switcher, which requests a URL for a language that is
        by definition not the current one).
        """
        lang = language or get_language()
        live = (self.live_i18n or {}).get(lang) or {}
        slug = live.get("public_slug") or live.get("slug")
        if not slug:
            if not self.has_translation(lang):
                return "#"
            with switch_language(self, lang):
                slug = self.safe_translation_getter("public_slug") or self.safe_translation_getter("slug")
        if not slug:
            return "#"
        with override(lang):
            return reverse("compare:detail", kwargs={"slug": slug})

    def _current_values_for(self, language: str) -> dict:
        """
        Current (draft) translation values for one language.

        Only used as the legacy fallback when no snapshot exists at all -
        hence the same language throughout: ``has_translation(language)``
        must be checked first, because ``safe_translation_getter()`` would
        otherwise silently substitute PARLER_LANGUAGES' fallback language.
        """
        if not self.has_translation(language):
            return {}
        with switch_language(self, language):
            get = self.safe_translation_getter
            return {
                "slug": get("slug"),
                "public_slug": get("public_slug"),
                "title": get("title"),
                "intro": get("intro"),
                "body": get("body"),
            }

    def get_display_value(self, field: str, language: str | None = None):
        """
        Snapshot-first public value, mirroring Guide/Prompt/UseCase.

        Beta 11.9 added this to Comparison: until then the public detail and
        list templates read ``obj.title``/``obj.intro``/``obj.body`` - raw
        parler descriptors, i.e. the *current draft* - while only the SEO
        context went through the live snapshot. That was a parent-level
        draft leak independent of the tool entries.
        """
        lang = language or get_language()
        value = self.get_live_value(field, lang)
        if value is not None:
            return value
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

    def build_live_entries(self) -> list:
        """
        The ordered, JSON-serialisable snapshot of this comparison's tool
        entries, built from the currently saved rows.

        Called only from :meth:`on_after_publish`, i.e. exactly at the
        moment the saved draft *becomes* the published state - never on a
        plain save, review, approval or rework, and never from a public
        view.

        Deliberately stores only ``tool_id`` rather than the tool's name or
        slug: Tool has no editorial draft state at all (its sole public gate
        is ``published_at <= now``), so its values are globally live
        everywhere on the site. Freezing them here would make published
        comparisons drift out of sync with the tool's own page for no
        security gain - the entry *selection* and *order* are the editorial
        decision, and those are what this snapshot pins.
        """
        entries = []
        for entry in self.tool_entries.order_by("position", "pk"):
            translations = {}
            for code in entry.get_available_languages():
                translations[code] = {
                    field: (
                        entry.safe_translation_getter(
                            field, language_code=code, any_language=False
                        )
                        or ""
                    )
                    for field in ENTRY_LIVE_FIELDS
                }
            entries.append(
                {
                    "tool_id": entry.tool_id,
                    "position": entry.position,
                    "translations": translations,
                }
            )
        return entries

    def on_after_publish(self):
        self.is_published = True
        if not self.published_at:
            self.published_at = timezone.now()
        for lang in self.get_available_languages():
            with switch_language(self, lang):
                if self.slug and self.public_slug != self.slug:
                    self.public_slug = self.slug
        # Beta 11.9: freeze the entries alongside the parent snapshot, so a
        # republish activates parent text and entry structure together.
        self.live_entries = self.build_live_entries()

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
