"""
Beta 11.10 group B/E: the preview's language contract.

The language comes from the URL, is validated against the supported content
languages, and is fail-closed everywhere: the comparison is read from the
requested language's stored translation or not at all. There is no
``PARLER_LANGUAGES`` fallback and no ambient-language leak.
"""
from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from compare.tests.draft_preview_fixtures import make_draft_comparison, make_user


def preview_url(comparison_pk, language_code):
    return reverse("admin:compare_comparison_draft_preview", args=[comparison_pk, language_code])


class PreviewLanguageIsolationTests(TestCase):
    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.editor = make_user("lang-editor", group="Editor")
        self.client.force_login(self.editor)

    def _bilingual_comparison(self):
        comparison = make_draft_comparison(
            self.editor,
            slug="bilingual-en",
            title="English Title",
            intro="English intro",
            body="<p>English body</p>",
        )
        comparison.create_translation(
            "de",
            slug="bilingual-de",
            title="Deutscher Titel",
            intro="Deutsches Intro",
            body="<p>Deutscher Text</p>",
        )
        return comparison

    def test_english_preview_shows_only_english(self):
        comparison = self._bilingual_comparison()
        resp = self.client.get(preview_url(comparison.pk, "en"))
        self.assertContains(resp, "English Title")
        self.assertNotContains(resp, "Deutscher Titel")

    def test_german_preview_shows_only_german(self):
        comparison = self._bilingual_comparison()
        resp = self.client.get(preview_url(comparison.pk, "de"))
        self.assertContains(resp, "Deutscher Titel")
        self.assertNotContains(resp, "English Title")

    def test_missing_german_translation_never_falls_back_to_english(self):
        comparison = make_draft_comparison(
            self.editor, slug="en-only-en", title="Only English Title"
        )
        resp = self.client.get(preview_url(comparison.pk, "de"))
        self.assertEqual(resp.status_code, 404)
        self.assertNotContains(resp, "Only English Title", status_code=404)

    def test_in_memory_only_translation_does_not_count_as_saved(self):
        """The Beta 11.4-confirmed Parler admin quirk: opening a language
        tab that has never been saved must not make the preview - or the
        button offering it - believe a translation exists."""
        comparison = make_draft_comparison(
            self.editor, slug="initialize-only-en", title="Initialize Only"
        )
        # Mirrors what TranslatableAdmin.get_object() does for an unsaved tab.
        comparison.set_current_language("de", initialize=True)
        self.assertTrue(comparison.has_translation("de"))  # the misleading signal

        resp = self.client.get(preview_url(comparison.pk, "de"))
        self.assertEqual(resp.status_code, 404)

    def test_page_chrome_is_rendered_in_the_requested_language(self):
        comparison = self._bilingual_comparison()
        html = self.client.get(preview_url(comparison.pk, "de")).content.decode()
        self.assertIn('href="/de/compare/"', html)

    def test_ambient_language_is_restored_after_the_request(self):
        comparison = self._bilingual_comparison()
        translation.activate("en")
        self.client.get(preview_url(comparison.pk, "de"))
        self.assertEqual(translation.get_language(), "en")

    def test_ambient_language_does_not_decide_the_preview_language(self):
        comparison = self._bilingual_comparison()
        translation.activate("de")
        resp = self.client.get(preview_url(comparison.pk, "en"))
        self.assertContains(resp, "English Title")
        self.assertNotContains(resp, "Deutscher Titel")

    def test_no_german_to_english_fallback(self):
        comparison = make_draft_comparison(
            self.editor, slug="de-only-de", title="Deutsch Only", language="de",
        )
        resp = self.client.get(preview_url(comparison.pk, "en"))
        self.assertEqual(resp.status_code, 404)
        self.assertNotContains(resp, "Deutsch Only", status_code=404)
