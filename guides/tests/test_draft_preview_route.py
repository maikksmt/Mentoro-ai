"""
Beta 11.4 group A: the draft-preview route's HTTP contract.

The endpoint is read-only, fail-closed on every input it does not fully
understand, and must never be cacheable or indexable.
"""
from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from guides.tests.draft_preview_fixtures import make_draft_guide, make_user


def preview_url(guide_pk, language_code):
    return reverse("admin:guides_guide_draft_preview", args=[guide_pk, language_code])


class DraftPreviewRouteTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.editor = make_user("route-editor", group="Editor")
        cls.guide = make_draft_guide(
            cls.editor, slug="route-guide-en", title="Route Guide"
        )

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.client.force_login(self.editor)

    def test_valid_get_renders(self):
        resp = self.client.get(preview_url(self.guide.pk, "en"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Route Guide")

    def test_post_is_rejected_with_405(self):
        resp = self.client.post(preview_url(self.guide.pk, "en"))
        self.assertEqual(resp.status_code, 405)

    def test_put_patch_and_delete_are_rejected_with_405(self):
        url = preview_url(self.guide.pk, "en")
        for method in ("put", "patch", "delete"):
            with self.subTest(method=method):
                resp = getattr(self.client, method)(url)
                self.assertEqual(resp.status_code, 405)

    def test_unknown_object_id_is_404(self):
        resp = self.client.get(preview_url(self.guide.pk + 9999, "en"))
        self.assertEqual(resp.status_code, 404)

    def test_non_numeric_object_id_is_404(self):
        resp = self.client.get(
            reverse("admin:guides_guide_draft_preview", args=["not-a-pk", "en"])
        )
        self.assertEqual(resp.status_code, 404)

    def test_unsupported_language_is_404(self):
        resp = self.client.get(preview_url(self.guide.pk, "fr"))
        self.assertEqual(resp.status_code, 404)

    def test_missing_translation_is_404_and_never_falls_back(self):
        # The guide only has an English translation.
        resp = self.client.get(preview_url(self.guide.pk, "de"))
        self.assertEqual(resp.status_code, 404)

    def test_response_is_marked_noindex_nofollow(self):
        resp = self.client.get(preview_url(self.guide.pk, "en"))
        self.assertEqual(resp["X-Robots-Tag"], "noindex,nofollow")
        self.assertContains(resp, 'content="noindex,nofollow"')

    def test_response_is_not_cacheable(self):
        resp = self.client.get(preview_url(self.guide.pk, "en"))
        cache_control = resp["Cache-Control"]
        for directive in ("no-store", "private", "no-cache", "max-age=0"):
            with self.subTest(directive=directive):
                self.assertIn(directive, cache_control)
        self.assertEqual(resp["Pragma"], "no-cache")

    def test_content_language_matches_the_requested_language(self):
        resp = self.client.get(preview_url(self.guide.pk, "en"))
        self.assertEqual(resp["Content-Language"], "en")
