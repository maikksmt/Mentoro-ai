"""
Beta 11.7 group C: the widened status rule must stay fail-closed per
language.

``live_i18n`` is keyed by language code, so a use case published in English
only has no public revision in German - and adding a German draft
translation afterwards must not change that until it is published.
"""
from django.conf import settings
from django.test import TestCase
from django.utils import translation

from usecases.models import UseCase
from usecases.tests.live_visibility_fixtures import (
    add_translation,
    make_usecase,
    make_user,
    publish,
    save_draft_edit,
    start_review_round,
)


class LanguageIsolationDuringReviewTests(TestCase):
    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.author = make_user("uc-lang-author")

    def _visible(self, usecase, language):
        return UseCase.objects.visible_in_language(language).filter(pk=usecase.pk).exists()

    def _bilingual_published(self):
        usecase = make_usecase(
            slug="bi-live-en", title="English Live", intro="EN intro",
            body="EN body", outro="EN outro", author=self.author,
        )
        add_translation(
            usecase, "de", slug="bi-live-de", title="Deutsch Live",
            intro="DE intro", body="DE body", outro="DE outro",
        )
        return publish(usecase, self.author)

    def test_english_only_usecase_is_visible_in_english(self):
        usecase = make_usecase(slug="lang-en-only", title="EN Only", author=self.author)
        published = publish(usecase, self.author)
        self.assertTrue(self._visible(published, "en"))

    def test_english_only_usecase_is_invisible_in_german(self):
        usecase = make_usecase(slug="lang-en-only-2", title="EN Only 2", author=self.author)
        published = publish(usecase, self.author)
        self.assertFalse(self._visible(published, "de"))

    def test_german_draft_added_after_publish_does_not_become_visible(self):
        """A translation created after the publish has no DE snapshot entry -
        it is a draft and must stay invisible under /de/."""
        usecase = make_usecase(slug="lang-late-de-en", title="EN First", author=self.author)
        publish(usecase, self.author)
        add_translation(usecase, "de", slug="lang-late-de-de", title="Spaeter Entwurf")

        refreshed = UseCase.objects.get(pk=usecase.pk)
        self.assertNotIn("de", refreshed.live_i18n)
        self.assertFalse(self._visible(refreshed, "de"))
        self.assertEqual(self.client.get("/de/usecases/lang-late-de-de/").status_code, 404)

    def test_german_draft_does_not_fall_back_to_english_content(self):
        usecase = make_usecase(slug="lang-nofallback-en", title="English Fallback Probe", author=self.author)
        publish(usecase, self.author)
        add_translation(usecase, "de", slug="lang-nofallback-de", title="Deutscher Entwurf")

        resp = self.client.get("/de/usecases/lang-nofallback-de/")
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn("English Fallback Probe", resp.content.decode())

    def test_both_published_languages_resolve_under_their_own_slug(self):
        self._bilingual_published()
        self.assertEqual(self.client.get("/en/usecases/bi-live-en/").status_code, 200)
        self.assertEqual(self.client.get("/de/usecases/bi-live-de/").status_code, 200)

    def test_no_cross_language_slug_resolution(self):
        self._bilingual_published()
        self.assertEqual(self.client.get("/de/usecases/bi-live-en/").status_code, 404)
        self.assertEqual(self.client.get("/en/usecases/bi-live-de/").status_code, 404)

    def test_english_draft_edit_leaves_german_public_output_untouched(self):
        usecase = self._bilingual_published()
        save_draft_edit(usecase, "en", title="English Draft Edit")
        start_review_round(usecase, self.author)

        german = self.client.get("/de/usecases/bi-live-de/")
        self.assertEqual(german.status_code, 200)
        html = german.content.decode()
        self.assertIn("Deutsch Live", html)
        self.assertNotIn("English Draft Edit", html)

    def test_german_draft_edit_leaves_english_public_output_untouched(self):
        usecase = self._bilingual_published()
        save_draft_edit(usecase, "de", title="Deutscher Entwurf Edit")
        start_review_round(usecase, self.author)

        english = self.client.get("/en/usecases/bi-live-en/")
        self.assertEqual(english.status_code, 200)
        html = english.content.decode()
        self.assertIn("English Live", html)
        self.assertNotIn("Deutscher Entwurf Edit", html)

    def test_both_languages_keep_serving_their_snapshot_during_review(self):
        usecase = self._bilingual_published()
        save_draft_edit(usecase, "en", title="EN Draft")
        save_draft_edit(usecase, "de", title="DE Entwurf")
        start_review_round(usecase, self.author)

        english = self.client.get("/en/usecases/bi-live-en/").content.decode()
        german = self.client.get("/de/usecases/bi-live-de/").content.decode()
        self.assertIn("English Live", english)
        self.assertNotIn("EN Draft", english)
        self.assertIn("Deutsch Live", german)
        self.assertNotIn("DE Entwurf", german)

    def test_language_resolution_does_not_leak_between_consecutive_requests(self):
        """Each request resolves its own language from the URL prefix; a
        preceding request in another language must not colour the next one."""
        self._bilingual_published()

        german = self.client.get("/de/usecases/bi-live-de/")
        english = self.client.get("/en/usecases/bi-live-en/")

        self.assertEqual(german.status_code, 200)
        self.assertEqual(english.status_code, 200)
        self.assertIn("Deutsch Live", german.content.decode())
        self.assertNotIn("English Live", german.content.decode())
        self.assertIn("English Live", english.content.decode())
        self.assertNotIn("Deutsch Live", english.content.decode())

    def test_hreflang_only_lists_languages_with_a_published_revision(self):
        usecase = make_usecase(slug="hreflang-en", title="Hreflang EN", author=self.author)
        publish(usecase, self.author)
        add_translation(usecase, "de", slug="hreflang-de-draft", title="Hreflang DE Entwurf")

        alternates = self.client.get("/en/usecases/hreflang-en/").context["seo"].alternates
        for alt in alternates:
            with self.subTest(lang=alt.lang):
                self.assertNotIn("hreflang-de-draft", alt.url)
