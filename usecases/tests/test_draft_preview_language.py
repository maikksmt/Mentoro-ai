"""
Beta 11.8 group E: the preview's language contract.

The language comes from the URL, is validated against the supported content
languages, and is fail-closed everywhere: the usecase is read from the
requested language's stored translation or not at all. There is no
``PARLER_LANGUAGES`` fallback and no ambient-language leak.
"""
from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from usecases.tests.draft_preview_fixtures import make_draft_usecase, make_user


def preview_url(usecase_pk, language_code):
    return reverse("admin:usecases_usecase_draft_preview", args=[usecase_pk, language_code])


class PreviewLanguageIsolationTests(TestCase):
    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.editor = make_user("lang-editor", group="Editor")
        self.client.force_login(self.editor)

    def _bilingual_usecase(self):
        usecase = make_draft_usecase(
            self.editor,
            slug="bilingual-en",
            title="English Title",
            intro="English intro",
            body="<p>English body</p>",
            outro="<p>English outro</p>",
        )
        usecase.create_translation(
            "de",
            slug="bilingual-de",
            title="Deutscher Titel",
            intro="Deutsches Intro",
            body="<p>Deutscher Text</p>",
            outro="<p>Deutscher Ausklang</p>",
            persona="",
        )
        return usecase

    def test_english_preview_shows_only_english(self):
        usecase = self._bilingual_usecase()
        resp = self.client.get(preview_url(usecase.pk, "en"))
        self.assertContains(resp, "English Title")
        self.assertNotContains(resp, "Deutscher Titel")

    def test_german_preview_shows_only_german(self):
        usecase = self._bilingual_usecase()
        resp = self.client.get(preview_url(usecase.pk, "de"))
        self.assertContains(resp, "Deutscher Titel")
        self.assertNotContains(resp, "English Title")

    def test_missing_german_translation_never_falls_back_to_english(self):
        usecase = make_draft_usecase(
            self.editor, slug="en-only-en", title="Only English Title"
        )
        resp = self.client.get(preview_url(usecase.pk, "de"))
        self.assertEqual(resp.status_code, 404)
        self.assertNotContains(resp, "Only English Title", status_code=404)

    def test_in_memory_only_translation_does_not_count_as_saved(self):
        """The Beta 11.4-confirmed Parler admin quirk: opening a language
        tab that has never been saved must not make the preview - or the
        button offering it - believe a translation exists."""
        usecase = make_draft_usecase(
            self.editor, slug="initialize-only-en", title="Initialize Only"
        )
        # Mirrors what TranslatableAdmin.get_object() does for an unsaved tab.
        usecase.set_current_language("de", initialize=True)
        self.assertTrue(usecase.has_translation("de"))  # the misleading signal

        resp = self.client.get(preview_url(usecase.pk, "de"))
        self.assertEqual(resp.status_code, 404)

    def test_page_chrome_is_rendered_in_the_requested_language(self):
        usecase = self._bilingual_usecase()
        html = self.client.get(preview_url(usecase.pk, "de")).content.decode()
        self.assertIn("Anwendungsf", html)  # "Anwendungsfälle" (Use cases)
        self.assertIn('href="/de/usecases/"', html)

    def test_ambient_language_is_restored_after_the_request(self):
        usecase = self._bilingual_usecase()
        translation.activate("en")
        self.client.get(preview_url(usecase.pk, "de"))
        self.assertEqual(translation.get_language(), "en")

    def test_ambient_language_does_not_decide_the_preview_language(self):
        usecase = self._bilingual_usecase()
        translation.activate("de")
        resp = self.client.get(preview_url(usecase.pk, "en"))
        self.assertContains(resp, "English Title")
        self.assertNotContains(resp, "Deutscher Titel")

    def test_no_german_to_english_fallback(self):
        usecase = make_draft_usecase(
            self.editor, slug="de-only-de", title="Deutsch Only", language="de",
        )
        resp = self.client.get(preview_url(usecase.pk, "en"))
        self.assertEqual(resp.status_code, 404)
        self.assertNotContains(resp, "Deutsch Only", status_code=404)
