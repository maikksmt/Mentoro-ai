"""
Beta 11.4 group D: the preview's language contract.

The language comes from the URL, is validated against the supported content
languages, and is fail-closed everywhere: guide, sections and items are all
read from the requested language's stored translation or not at all. There is
no ``PARLER_LANGUAGES`` fallback and no ambient-language leak.
"""
from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from guides.tests.draft_preview_fixtures import (
    add_item,
    add_section,
    make_draft_guide,
    make_user,
)


def preview_url(guide_pk, language_code):
    return reverse("admin:guides_guide_draft_preview", args=[guide_pk, language_code])


class PreviewLanguageIsolationTests(TestCase):
    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.editor = make_user("lang-editor", group="Editor")
        self.client.force_login(self.editor)

    def _bilingual_guide(self):
        guide = make_draft_guide(
            self.editor,
            slug="bilingual-en",
            title="English Title",
            intro="English intro",
            body="<p>English body</p>",
        )
        guide.create_translation(
            "de",
            slug="bilingual-de",
            title="Deutscher Titel",
            intro="Deutsches Intro",
            body="<p>Deutscher Text</p>",
        )
        return guide

    def test_english_preview_shows_only_english(self):
        guide = self._bilingual_guide()
        resp = self.client.get(preview_url(guide.pk, "en"))
        self.assertContains(resp, "English Title")
        self.assertNotContains(resp, "Deutscher Titel")

    def test_german_preview_shows_only_german(self):
        guide = self._bilingual_guide()
        resp = self.client.get(preview_url(guide.pk, "de"))
        self.assertContains(resp, "Deutscher Titel")
        self.assertNotContains(resp, "English Title")

    def test_missing_german_translation_never_falls_back_to_english(self):
        guide = make_draft_guide(
            self.editor, slug="en-only-en", title="Only English Title"
        )
        resp = self.client.get(preview_url(guide.pk, "de"))
        self.assertEqual(resp.status_code, 404)
        self.assertNotContains(resp, "Only English Title", status_code=404)

    def test_section_without_the_requested_language_is_omitted(self):
        guide = self._bilingual_guide()
        add_section(
            guide,
            order=0,
            translations={"en": ("English Only Section", "<p>en sec</p>")},
        )
        resp = self.client.get(preview_url(guide.pk, "de"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "English Only Section")

    def test_item_without_the_requested_language_is_omitted(self):
        guide = self._bilingual_guide()
        section = add_section(
            guide,
            order=0,
            translations={
                "en": ("Section EN", "<p>en</p>"),
                "de": ("Section DE", "<p>de</p>"),
            },
        )
        add_item(
            section, order=0, translations={"en": ("English Only Item", "en teaser")}
        )
        resp = self.client.get(preview_url(guide.pk, "de"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Section DE")
        self.assertNotContains(resp, "English Only Item")

    def test_bilingual_section_and_item_show_the_requested_language_only(self):
        guide = self._bilingual_guide()
        section = add_section(
            guide,
            order=0,
            translations={
                "en": ("Section English", "<p>en body</p>"),
                "de": ("Sektion Deutsch", "<p>de body</p>"),
            },
        )
        add_item(
            section,
            order=0,
            translations={
                "en": ("Item English", "en teaser"),
                "de": ("Element Deutsch", "de teaser"),
            },
        )
        resp = self.client.get(preview_url(guide.pk, "de"))
        self.assertContains(resp, "Sektion Deutsch")
        self.assertContains(resp, "Element Deutsch")
        self.assertNotContains(resp, "Section English")
        self.assertNotContains(resp, "Item English")

    def test_page_chrome_is_rendered_in_the_requested_language(self):
        guide = self._bilingual_guide()
        html = self.client.get(preview_url(guide.pk, "de")).content.decode()
        # The breadcrumb root is a {% trans %} string; German is "Start"
        # too, so assert on a navigation label that actually differs.
        self.assertIn("Anwendungsf", html)  # "Anwendungsfälle" (Use cases)
        self.assertIn('href="/de/guides/"', html)

    def test_ambient_language_is_restored_after_the_request(self):
        guide = self._bilingual_guide()
        translation.activate("en")
        self.client.get(preview_url(guide.pk, "de"))
        self.assertEqual(translation.get_language(), "en")

    def test_ambient_language_does_not_decide_the_preview_language(self):
        guide = self._bilingual_guide()
        translation.activate("de")
        resp = self.client.get(preview_url(guide.pk, "en"))
        self.assertContains(resp, "English Title")
        self.assertNotContains(resp, "Deutscher Titel")
