"""
Beta 11.7 groups H + cache: the persona field's live gate, and the
guarantee that no public cache ever picks up a draft value.

``persona`` is the field that made this slice's visibility change unsafe
until now: ``templates/usecases/list.html`` rendered it straight off the
current translation (``obj.persona``), with no snapshot involvement, so any
rule keeping a use case listed while it sat in review would have published
whatever the author had just typed. It is now part of
``LIVE_SNAPSHOT_FIELDS`` and the card reads ``display_persona``.

The one public cache in this project is
``core.services.get_public_inventory`` (``mentoroai:public-inventory:<v>:<lang>``);
there is no use-case detail cache, related-content cache or template
fragment cache.
"""
from django.conf import settings
from django.core.cache import cache
from django.test import TestCase
from django.utils import translation

from core.services import get_public_inventory
from usecases.models import UseCase
from usecases.tests.live_visibility_fixtures import (
    make_usecase,
    make_user,
    publish,
    save_draft_edit,
    start_review_round,
)

LIVE_PERSONA = "Live Persona Marker"
DRAFT_PERSONA = "Draft Persona Marker"


class PersonaIsLiveGatedTests(TestCase):
    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.author = make_user("uc-persona-author")

    def test_persona_is_part_of_the_live_snapshot_contract(self):
        self.assertIn("persona", UseCase.LIVE_SNAPSHOT_FIELDS)

    def test_publishing_writes_persona_into_the_snapshot(self):
        usecase = make_usecase(
            slug="persona-snapshot", title="Persona Snapshot",
            persona=LIVE_PERSONA, author=self.author,
        )
        published = publish(usecase, self.author)
        self.assertEqual(published.live_i18n["en"]["persona"], LIVE_PERSONA)

    def test_display_persona_returns_the_published_value_during_review(self):
        usecase = make_usecase(
            slug="persona-review", title="Persona Review",
            persona=LIVE_PERSONA, author=self.author,
        )
        publish(usecase, self.author)
        save_draft_edit(usecase, "en", persona=DRAFT_PERSONA)
        in_review = start_review_round(usecase, self.author)

        in_review.set_current_language("en")
        self.assertEqual(in_review.persona, DRAFT_PERSONA, "raw descriptor is the draft")
        self.assertEqual(in_review.display_persona, LIVE_PERSONA, "public getter is the snapshot")

    def test_list_page_shows_the_published_persona_not_the_draft(self):
        usecase = make_usecase(
            slug="persona-list", title="Persona List",
            persona=LIVE_PERSONA, author=self.author,
        )
        publish(usecase, self.author)
        save_draft_edit(usecase, "en", persona=DRAFT_PERSONA)
        start_review_round(usecase, self.author)

        html = self.client.get("/en/usecases/").content.decode()
        self.assertIn(LIVE_PERSONA, html)
        self.assertNotIn(DRAFT_PERSONA, html)

    def test_never_published_persona_never_reaches_the_list_page(self):
        make_usecase(
            slug="persona-neverpub", title="Persona Never Published",
            persona=DRAFT_PERSONA, author=self.author,
        )
        html = self.client.get("/en/usecases/").content.decode()
        self.assertNotIn(DRAFT_PERSONA, html)

    def test_snapshot_written_before_this_slice_fails_closed(self):
        """A pre-Beta-11.7 snapshot carries no persona key; the public getter
        must resolve it to "" rather than falling through to the draft."""
        usecase = make_usecase(
            slug="persona-legacy", title="Persona Legacy",
            persona=DRAFT_PERSONA, author=self.author,
        )
        published = publish(usecase, self.author)
        legacy_snapshot = dict(published.live_i18n["en"])
        legacy_snapshot.pop("persona")
        published.live_i18n = {"en": legacy_snapshot}
        published.save(update_fields=["live_i18n"])

        refreshed = UseCase.objects.get(pk=published.pk)
        refreshed.set_current_language("en")
        self.assertEqual(refreshed.display_persona, "")

        html = self.client.get("/en/usecases/").content.decode()
        self.assertNotIn(DRAFT_PERSONA, html)

    def test_persona_is_still_not_indexed_by_search(self):
        from search.adapters.usecases import USE_CASE_SEARCH_FIELDS

        self.assertNotIn("persona", [field.public_field for field in USE_CASE_SEARCH_FIELDS])

    def test_detail_template_does_not_render_persona(self):
        usecase = make_usecase(
            slug="persona-detail", title="Persona Detail",
            persona=LIVE_PERSONA, author=self.author,
        )
        publish(usecase, self.author)
        html = self.client.get("/en/usecases/persona-detail/").content.decode()
        self.assertNotIn(LIVE_PERSONA, html)


class PublicCacheNeverHoldsADraftValueTests(TestCase):
    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        cache.clear()
        self.addCleanup(cache.clear)
        self.author = make_user("uc-cache-author")

        self.usecase = make_usecase(
            slug="cache-live-slug", title="Cache Live Title",
            intro="Cache live intro", body="Cache live body",
            persona=LIVE_PERSONA, author=self.author,
        )
        publish(self.usecase, self.author)

    def _list_html(self):
        return self.client.get("/en/usecases/").content.decode()

    def test_live_value_survives_a_draft_edit_and_a_cache_clear(self):
        self.assertIn("Cache Live Title", self._list_html())

        save_draft_edit(self.usecase, "en", title="Cache Draft Title", persona=DRAFT_PERSONA)
        start_review_round(self.usecase, self.author)

        self.assertIn("Cache Live Title", self._list_html())
        self.assertNotIn("Cache Draft Title", self._list_html())

        cache.clear()
        html = self._list_html()
        self.assertIn("Cache Live Title", html)
        self.assertNotIn("Cache Draft Title", html)
        self.assertNotIn(DRAFT_PERSONA, html)

    def test_public_inventory_cache_holds_no_draft_value(self):
        save_draft_edit(self.usecase, "en", title="Cache Draft Title", persona=DRAFT_PERSONA)
        start_review_round(self.usecase, self.author)

        get_public_inventory("en")
        for language in ("en", "de"):
            key = f"mentoroai:public-inventory:v5:{language}"
            with self.subTest(language=language):
                cached = repr(cache.get(key))
                self.assertNotIn("Cache Draft Title", cached)
                self.assertNotIn(DRAFT_PERSONA, cached)

    def test_detail_page_stays_on_live_values_across_repeated_reads(self):
        save_draft_edit(self.usecase, "en", title="Cache Draft Title")
        start_review_round(self.usecase, self.author)

        for _ in range(3):
            html = self.client.get("/en/usecases/cache-live-slug/").content.decode()
            self.assertIn("Cache Live Title", html)
            self.assertNotIn("Cache Draft Title", html)
