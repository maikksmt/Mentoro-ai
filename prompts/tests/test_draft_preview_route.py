"""
Beta 11.5 group A: the draft-preview route's HTTP contract.

Mirrors ``guides/tests/test_draft_preview_route.py`` (Beta 11.4).
"""
from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from prompts.tests.draft_preview_fixtures import make_draft_prompt, make_user


def preview_url(prompt_pk, language_code):
    return reverse("admin:prompts_prompt_draft_preview", args=[prompt_pk, language_code])


class DraftPreviewRouteTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.editor = make_user("route-editor", group="Editor")
        cls.prompt = make_draft_prompt(
            cls.editor, slug="route-prompt-en", title="Route Prompt"
        )

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.client.force_login(self.editor)

    def test_valid_get_renders(self):
        resp = self.client.get(preview_url(self.prompt.pk, "en"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Route Prompt")

    def test_head_is_allowed(self):
        resp = self.client.head(preview_url(self.prompt.pk, "en"))
        self.assertEqual(resp.status_code, 200)

    def test_post_is_rejected_with_405(self):
        resp = self.client.post(preview_url(self.prompt.pk, "en"))
        self.assertEqual(resp.status_code, 405)

    def test_put_patch_and_delete_are_rejected_with_405(self):
        url = preview_url(self.prompt.pk, "en")
        for method in ("put", "patch", "delete"):
            with self.subTest(method=method):
                resp = getattr(self.client, method)(url)
                self.assertEqual(resp.status_code, 405)

    def test_unknown_object_id_is_404(self):
        resp = self.client.get(preview_url(self.prompt.pk + 9999, "en"))
        self.assertEqual(resp.status_code, 404)

    def test_non_numeric_object_id_is_404(self):
        resp = self.client.get(
            reverse("admin:prompts_prompt_draft_preview", args=["not-a-pk", "en"])
        )
        self.assertEqual(resp.status_code, 404)

    def test_unsupported_language_is_404(self):
        resp = self.client.get(preview_url(self.prompt.pk, "fr"))
        self.assertEqual(resp.status_code, 404)

    def test_missing_translation_is_404_and_never_falls_back(self):
        # The prompt only has an English translation.
        resp = self.client.get(preview_url(self.prompt.pk, "de"))
        self.assertEqual(resp.status_code, 404)

    def test_response_is_marked_noindex_nofollow(self):
        resp = self.client.get(preview_url(self.prompt.pk, "en"))
        self.assertEqual(resp["X-Robots-Tag"], "noindex,nofollow")
        self.assertContains(resp, 'content="noindex,nofollow"')

    def test_response_is_not_cacheable(self):
        resp = self.client.get(preview_url(self.prompt.pk, "en"))
        cache_control = resp["Cache-Control"]
        for directive in ("no-store", "private", "no-cache", "max-age=0"):
            with self.subTest(directive=directive):
                self.assertIn(directive, cache_control)
        self.assertEqual(resp["Pragma"], "no-cache")

    def test_content_language_matches_the_requested_language(self):
        resp = self.client.get(preview_url(self.prompt.pk, "en"))
        self.assertEqual(resp["Content-Language"], "en")

    def test_no_open_graph_url_meta_tag_is_rendered(self):
        """A preview has no canonical URL to publish, so og:url must be
        absent entirely - not an empty placeholder (see
        templates/partials/_seo_meta.html)."""
        html = self.client.get(preview_url(self.prompt.pk, "en")).content.decode()
        self.assertNotIn('property="og:url"', html)
        self.assertNotIn("property='og:url'", html)

    def test_no_canonical_link_is_rendered(self):
        html = self.client.get(preview_url(self.prompt.pk, "en")).content.decode()
        self.assertNotIn('rel="canonical"', html)

    def test_no_hreflang_alternate_links_are_rendered(self):
        html = self.client.get(preview_url(self.prompt.pk, "en")).content.decode()
        self.assertNotIn("hreflang=", html)

    def test_no_json_ld_is_rendered(self):
        html = self.client.get(preview_url(self.prompt.pk, "en")).content.decode()
        self.assertNotIn("application/ld+json", html)
