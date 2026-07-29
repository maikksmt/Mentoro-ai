from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _, get_language
from parler.models import TranslatableModel, TranslatedFields
from parler.utils.context import switch_language

from catalog.models import Tool
from core.models.editorial import EditorialManager, EditorialQuerySet, EditorialWorkflowMixin


class UseCaseQuerySet(EditorialQuerySet):
    #: Beta 11.11D1 removed this queryset's local ``LIVE_EDITING_STATUSES``
    #: and ``visible_on_site()`` override entirely; both now come from
    #: ``EditorialQuerySet``.
    #:
    #: The override existed only because Beta 11.7A took the "rework must
    #: stay public" decision for use cases before the shared base adopted it
    #: (Beta 11.11B2A did, making the two identical apart from the constant).
    #: D1 then had to widen the set again - ``draft`` is now where an
    #: automatic invalidation lands - and had to replace the publication
    #: proof (``last_published_revision_id``, which only ``core.admin``'s
    #: publish path ever writes) with ``is_published`` plus a real snapshot.
    #: Keeping a divergent copy of that here would have silently frozen use
    #: cases on the pre-D1 rule and dropped every edited use case off the
    #: site the moment B2B2 moved it to ``draft``. See
    #: ``EditorialQuerySet.visible_on_site()`` for the full contract.

    def visible_in_language(self, language_code):
        """
        Public use cases that have an actual translation in language_code -
        never a fallback from another language (mirrors
        Prompt.objects.visible_in_language() from Beta 8.8). Unlike the
        .published manager's active_translations()-based fallback, this
        never lets an EN-only use case leak onto the DE list/homepage/detail
        resolution or vice versa.

        Status rule (Beta 11.7): visible_on_site() - published, or one of
        :attr:`LIVE_EDITING_STATUSES` with an existing live revision. That is
        the rule Guide and Prompt already use, plus rework (Beta 11.7A).
        It replaces the stricter published()
        this queryset carried until Beta 11.6, under which the admin's own
        auto-review guard (EditorialWorkflowAdminMixin._must_auto_review())
        took an edited use case's entire public page offline the moment an
        author saved a change, even though its published live_i18n snapshot
        was still intact (reproduced through the real admin POST in
        usecases/tests/test_published_edit_visibility.py).

        Widening the status rule is only safe because every field the public
        surfaces actually render now resolves through the live snapshot:
        LIVE_SNAPSHOT_FIELDS gained "persona" in the same slice, closing the
        one field (rendered by templates/usecases/list.html) that used to be
        read straight off the current draft translation. Archived use cases
        stay excluded - visible_on_site() never admits STATUS_ARCHIVED.

        The snapshot filter makes the language rule fail-closed, which
        ``translated()`` alone is not. ``translated()`` only asks whether a
        translation *row* exists, so a use case published in English and
        given a German translation afterwards counted as publicly visible in
        German and served that never-published draft under /de/ (confirmed by
        reproduction against the unchanged Beta 11.6 tree - a pre-existing
        defect, not one this slice introduced). Public visibility in a
        language now requires a published revision *in that language*:

        * ``live_i18n`` has an entry for ``language_code`` - published here.
        * ``live_i18n`` is entirely empty - a record predating the snapshot
          mechanism; the strict ``published()``-era behaviour is kept for it,
          matching core/projections.py::public_content_value()'s state C.
        * ``live_i18n`` is non-empty but lacks this language - published in
          other languages only, so there is no public revision here (state
          B). Excluded.

        Beta 11.11D1 moved that three-state language rule verbatim into the
        shared ``EditorialQuerySet.live_snapshot_language_q()`` so Guide and
        Prompt could adopt it too; the semantics here are unchanged.
        """
        return (
            self.visible_on_site()
            .filter(self.live_snapshot_language_q(language_code))
            .translated(language_code)
            .language(language_code)
            .distinct()
        )


UseCaseManager = EditorialManager.from_queryset(UseCaseQuerySet)


class UseCase(EditorialWorkflowMixin, TranslatableModel):
    #: Beta 11.7 added "persona": templates/usecases/list.html renders it on
    #: the public card, so it needs the same published-snapshot gate the
    #: other rendered fields have. Without it, widening visible_in_language()
    #: to visible_on_site() would have published the current draft persona of
    #: any use case sitting in review. Snapshots written before this slice
    #: carry no "persona" key and therefore resolve to "" (fail-closed) until
    #: the object is published again - see get_snapshot_field()'s state A.
    LIVE_SNAPSHOT_FIELDS = ("slug", "public_slug", "title", "intro", "body", "outro", "persona")
    live_i18n = models.JSONField(default=dict, blank=True)
    translations = TranslatedFields(
        title=models.CharField(_("Title"), max_length=200),
        intro=models.TextField(_("Intro"), blank=True),
        body=models.TextField(_("Body"), blank=True),
        outro=models.TextField(_("Outro"), blank=True),
        slug=models.SlugField(_("Slug"), max_length=220, unique=True),
        public_slug=models.SlugField(_("Public slug"), max_length=220, unique=True, null=True, blank=True),
        persona=models.CharField(_("Persona"), max_length=100),
    )

    tools = models.ManyToManyField(Tool, related_name="usecases", blank=True)

    objects = UseCaseManager()

    class Meta:
        verbose_name = _("Usecase")
        verbose_name_plural = _("Usecases")

    def __str__(self) -> str:
        return self.safe_translation_getter("title", any_language=True) or f"UseCase #{self.pk}"

    def _current_values_for(self, language: str):
        """
        Gibt die aktuellen Übersetzungswerte (Draft) für eine Sprache zurück.

        Nur als Legacy-Fallback verwendet, wenn kein Snapshot existiert -
        deshalb ausschließlich dieselbe Sprache: has_translation(language)
        muss zuerst geprüft werden, da safe_translation_getter() sonst über
        Parlers eigenen (PARLER_LANGUAGES-)Fallback stillschweigend eine
        andere Sprache liefern würde.
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
                "outro": get("outro"),
                "persona": get("persona"),
            }

    def get_display_value(self, field: str, language: str | None = None):
        lang = language or get_language()
        val = self.get_live_value(field, lang)
        if val is not None:
            return val
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

    @property
    def display_persona(self):
        return self.get_display_value("persona")

    def on_after_publish(self):
        self.is_published = True
        if not self.published_at:
            self.published_at = timezone.now()

        for lang in self.get_available_languages():
            with switch_language(self, lang):
                if self.slug and self.public_slug != self.slug:
                    self.public_slug = self.slug

    def get_absolute_url(self, language: str | None = None):
        """
        The public detail URL for ``language``, or ``"#"`` when there is no
        public revision in that language.

        Beta 11.7 made the missing-snapshot case fail closed. The three
        states mirror core/projections.py::public_content_value():

        * a snapshot entry for ``language`` exists - its slug is the sole
          public slug for that language;
        * ``live_i18n`` is entirely empty - a record predating the snapshot
          mechanism, so the current translation's slug still stands in;
        * ``live_i18n`` is non-empty but has no entry for ``language`` -
          published in other languages only. Falling back to the current
          translation here handed out a never-published draft slug, which
          ``localized_alternates()`` then advertised as an hreflang
          alternate pointing at a 404. Returning ``"#"`` makes that helper
          skip the language, matching what it already does for Comparison.
        """
        lang = language or get_language()
        snapshot = self.live_i18n or {}
        live = snapshot.get(lang or "", {})
        slug = live.get("public_slug") or live.get("slug")

        if not slug:
            if snapshot:
                return "#"
            from parler.utils.context import switch_language
            with switch_language(self, lang):
                slug = self.safe_translation_getter("public_slug") or self.safe_translation_getter("slug")
        if not slug:
            return "#"
        return reverse("usecases:detail", kwargs={"slug": slug})
