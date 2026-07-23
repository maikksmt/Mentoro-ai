"""
Beta 11.9 group E: the entry snapshot is per-language and fail-closed.

``live_entries`` stores one ``translations`` dict per entry, keyed by
language code. A language missing from that dict yields empty strings for
that entry - never another language's text, and never the draft row.
"""
from django.conf import settings
from django.test import TestCase
from django.utils import translation

from compare.models import Comparison
from compare.presentation import public_tool_entries
from compare.tests.live_snapshot_fixtures import (
    add_entry,
    add_entry_translation,
    add_translation,
    make_comparison,
    make_tool,
    make_user,
    publish,
    save_entry_draft_edit,
    start_review_round,
)


class EntryLanguageIsolationTests(TestCase):
    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.author = make_user("cmp-lang-author")
        self.tool = make_tool("lang-tool", "Lang Tool")

    def _bilingual(self):
        comparison = make_comparison(
            slug="lang-en", title="English Title", intro="EN intro", author=self.author
        )
        add_translation(
            comparison, "de", slug="lang-de", title="Deutscher Titel", intro="DE Intro"
        )
        entry = add_entry(
            comparison, self.tool, position=10,
            label="EN label", summary="<p>EN summary</p>",
        )
        add_entry_translation(
            entry, "de", label="DE Label", summary="<p>DE Zusammenfassung</p>"
        )
        return publish(comparison, self.author), entry

    def test_snapshot_stores_both_languages_separately(self):
        comparison, _entry = self._bilingual()
        snapshot = comparison.live_entries
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(sorted(snapshot[0]["translations"].keys()), ["de", "en"])
        self.assertEqual(snapshot[0]["translations"]["en"]["label"], "EN label")
        self.assertEqual(snapshot[0]["translations"]["de"]["label"], "DE Label")

    def test_english_page_shows_only_english_entry_text(self):
        self._bilingual()
        html = self.client.get("/en/compare/lang-en/").content.decode()
        self.assertIn("EN label", html)
        self.assertIn("EN summary", html)
        self.assertNotIn("DE Label", html)
        self.assertNotIn("DE Zusammenfassung", html)

    def test_german_page_shows_only_german_entry_text(self):
        self._bilingual()
        html = self.client.get("/de/compare/lang-de/").content.decode()
        self.assertIn("DE Label", html)
        self.assertIn("DE Zusammenfassung", html)
        self.assertNotIn("EN label", html)
        self.assertNotIn("EN summary", html)

    def test_english_entry_draft_edit_does_not_change_german(self):
        comparison, entry = self._bilingual()
        save_entry_draft_edit(entry, "en", summary="<p>EN draft summary</p>")
        start_review_round(comparison, self.author)

        html = self.client.get("/de/compare/lang-de/").content.decode()
        self.assertIn("DE Zusammenfassung", html)
        self.assertNotIn("EN draft summary", html)

    def test_german_entry_draft_edit_does_not_change_english(self):
        comparison, entry = self._bilingual()
        save_entry_draft_edit(entry, "de", summary="<p>DE Entwurf</p>")
        start_review_round(comparison, self.author)

        html = self.client.get("/en/compare/lang-en/").content.decode()
        self.assertIn("EN summary", html)
        self.assertNotIn("DE Entwurf", html)

    def test_entry_without_a_snapshot_in_the_language_yields_no_text(self):
        """An entry published in English only renders empty - never the
        English text - if the comparison itself is public in German."""
        comparison = make_comparison(
            slug="entrylang-en", title="EntryLang EN", author=self.author
        )
        add_translation(comparison, "de", slug="entrylang-de", title="EntryLang DE")
        # Entry has an EN translation only.
        add_entry(comparison, self.tool, position=10, summary="<p>EN only summary</p>")
        published = publish(comparison, self.author)

        entries = public_tool_entries(published, "de")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].summary, "")

        html = self.client.get("/de/compare/entrylang-de/").content.decode()
        self.assertNotIn("EN only summary", html)

    def test_german_draft_translation_added_after_publish_is_not_public(self):
        comparison = make_comparison(
            slug="latede-en", title="LateDE EN", author=self.author
        )
        add_entry(comparison, self.tool, position=10, summary="<p>EN summary</p>")
        published = publish(comparison, self.author)

        add_translation(published, "de", slug="latede-de", title="LateDE DE Entwurf")

        refreshed = Comparison.objects.get(pk=published.pk)
        self.assertNotIn("de", refreshed.live_i18n)
        self.assertFalse(
            Comparison.objects.visible_in_language("de").filter(pk=refreshed.pk).exists()
        )
        self.assertEqual(self.client.get("/de/compare/latede-de/").status_code, 404)

    def test_no_cross_language_slug_resolution(self):
        self._bilingual()
        self.assertEqual(self.client.get("/de/compare/lang-en/").status_code, 404)
        self.assertEqual(self.client.get("/en/compare/lang-de/").status_code, 404)

    def test_language_resolution_does_not_leak_between_consecutive_requests(self):
        """Each request resolves its own language from the URL prefix; a
        preceding request in another language must not colour the next one.

        (Deliberately not asserting the ambient language after the request:
        these are public i18n_patterns views, where LocaleMiddleware
        activates the request language for the thread - unlike the admin
        draft previews, which use a scoped ``translation.override()``.)"""
        self._bilingual()

        german = self.client.get("/de/compare/lang-de/")
        english = self.client.get("/en/compare/lang-en/")

        self.assertEqual(german.status_code, 200)
        self.assertEqual(english.status_code, 200)
        self.assertIn("DE Label", german.content.decode())
        self.assertNotIn("EN label", german.content.decode())
        self.assertIn("EN label", english.content.decode())
        self.assertNotIn("DE Label", english.content.decode())
