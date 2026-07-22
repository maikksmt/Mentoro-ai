"""
Beta 11.5 group D: the preview's language contract.

The language comes from the URL, is validated against the supported content
languages, and is fail-closed everywhere: the prompt is read from the
requested language's stored translation or not at all. There is no
``PARLER_LANGUAGES`` fallback and no ambient-language leak.
"""
from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from prompts.tests.draft_preview_fixtures import make_draft_prompt, make_user


def preview_url(prompt_pk, language_code):
    return reverse("admin:prompts_prompt_draft_preview", args=[prompt_pk, language_code])


class PreviewLanguageIsolationTests(TestCase):
    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.editor = make_user("lang-editor", group="Editor")
        self.client.force_login(self.editor)

    def _bilingual_prompt(self):
        prompt = make_draft_prompt(
            self.editor,
            slug="bilingual-en",
            title="English Title",
            intro="English intro",
            body="<p>English body</p>",
            outro="<p>English outro</p>",
        )
        prompt.create_translation(
            "de",
            slug="bilingual-de",
            title="Deutscher Titel",
            intro="Deutsches Intro",
            body="<p>Deutscher Text</p>",
            outro="<p>Deutscher Ausklang</p>",
        )
        return prompt

    def test_english_preview_shows_only_english(self):
        prompt = self._bilingual_prompt()
        resp = self.client.get(preview_url(prompt.pk, "en"))
        self.assertContains(resp, "English Title")
        self.assertNotContains(resp, "Deutscher Titel")

    def test_german_preview_shows_only_german(self):
        prompt = self._bilingual_prompt()
        resp = self.client.get(preview_url(prompt.pk, "de"))
        self.assertContains(resp, "Deutscher Titel")
        self.assertNotContains(resp, "English Title")

    def test_missing_german_translation_never_falls_back_to_english(self):
        prompt = make_draft_prompt(
            self.editor, slug="en-only-en", title="Only English Title"
        )
        resp = self.client.get(preview_url(prompt.pk, "de"))
        self.assertEqual(resp.status_code, 404)
        self.assertNotContains(resp, "Only English Title", status_code=404)

    def test_in_memory_only_translation_does_not_count_as_saved(self):
        """The Beta 11.4-confirmed Parler admin quirk: opening a language
        tab that has never been saved must not make the preview - or the
        button offering it - believe a translation exists."""
        prompt = make_draft_prompt(
            self.editor, slug="initialize-only-en", title="Initialize Only"
        )
        # Mirrors what TranslatableAdmin.get_object() does for an unsaved tab.
        prompt.set_current_language("de", initialize=True)
        self.assertTrue(prompt.has_translation("de"))  # the misleading signal

        resp = self.client.get(preview_url(prompt.pk, "de"))
        self.assertEqual(resp.status_code, 404)

    def test_page_chrome_is_rendered_in_the_requested_language(self):
        prompt = self._bilingual_prompt()
        html = self.client.get(preview_url(prompt.pk, "de")).content.decode()
        self.assertIn("Anwendungsf", html)  # "Anwendungsfälle" (Use cases)
        self.assertIn('href="/de/prompts/"', html)

    def test_copy_label_is_rendered_in_the_requested_language(self):
        prompt = self._bilingual_prompt()
        html = self.client.get(preview_url(prompt.pk, "de")).content.decode()
        # prompt_detail.html passes {% trans "Copy prompt" %} as the button's
        # copy_label -> German "Prompt kopieren" (see locale/de/.../django.po).
        self.assertIn("Prompt kopieren", html)

    def test_ambient_language_is_restored_after_the_request(self):
        prompt = self._bilingual_prompt()
        translation.activate("en")
        self.client.get(preview_url(prompt.pk, "de"))
        self.assertEqual(translation.get_language(), "en")

    def test_ambient_language_does_not_decide_the_preview_language(self):
        prompt = self._bilingual_prompt()
        translation.activate("de")
        resp = self.client.get(preview_url(prompt.pk, "en"))
        self.assertContains(resp, "English Title")
        self.assertNotContains(resp, "Deutscher Titel")
