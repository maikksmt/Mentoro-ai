"""
Beta 11.4 groups I/J: the admin change-form entry point, and the guarantee
that the preview route stays out of every public surface.
"""
from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from guides.tests.draft_preview_fixtures import make_draft_guide, make_user


class DraftPreviewButtonTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = make_user("btn-owner", group="Author")
        cls.other_author = make_user("btn-other", group="Author")
        cls.editor = make_user("btn-editor", group="Editor")
        cls.guide = make_draft_guide(
            cls.owner, slug="btn-guide-en", title="Button Guide"
        )
        cls.guide.create_translation(
            "de", slug="btn-guide-de", title="Button Guide DE", intro="i", body="b"
        )

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)

    def _change_url(self):
        return reverse("admin:guides_guide_change", args=[self.guide.pk])

    def _expected_link(self, language_code):
        return reverse(
            "admin:guides_guide_draft_preview", args=[self.guide.pk, language_code]
        )

    def test_button_is_present_on_the_change_form(self):
        self.client.force_login(self.owner)
        resp = self.client.get(self._change_url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Preview saved draft")
        self.assertContains(resp, self._expected_link("en"))

    def test_button_opens_in_a_new_tab_with_noopener(self):
        self.client.force_login(self.owner)
        html = self.client.get(self._change_url()).content.decode()
        anchor_start = html.index(self._expected_link("en"))
        anchor = html[anchor_start - 200 : anchor_start + 200]
        self.assertIn('target="_blank"', anchor)
        self.assertIn('rel="noopener"', anchor)

    def test_button_uses_the_active_parler_language_tab(self):
        self.client.force_login(self.owner)
        resp = self.client.get(self._change_url() + "?language=de")
        self.assertContains(resp, self._expected_link("de"))
        self.assertNotContains(resp, self._expected_link("en"))

    def test_button_is_absent_on_the_add_form(self):
        self.client.force_login(self.editor)
        resp = self.client.get(reverse("admin:guides_guide_add"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Preview saved draft")

    def test_button_is_absent_for_a_language_without_a_saved_translation(self):
        english_only = make_draft_guide(
            self.owner, slug="btn-en-only-en", title="English Only"
        )
        self.client.force_login(self.owner)
        url = reverse("admin:guides_guide_change", args=[english_only.pk])
        resp = self.client.get(url + "?language=de")
        self.assertNotContains(resp, "Preview saved draft")

    def test_button_is_absent_for_a_user_who_may_not_preview(self):
        # A non-owning Author may open the change form read-only, but must
        # not be offered a preview they are not allowed to open.
        self.client.force_login(self.other_author)
        resp = self.client.get(self._change_url())
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Preview saved draft")

    def test_existing_draft_versus_live_button_is_untouched(self):
        self.client.force_login(self.owner)
        resp = self.client.get(self._change_url())
        self.assertContains(
            resp, reverse("admin:guides_guide_diff", args=[self.guide.pk])
        )
        self.assertContains(resp, "Draft ↔ Live")


class PreviewStaysOutOfPublicSurfacesTests(TestCase):
    """Group J: the preview URL must never be advertised publicly."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.editor = make_user("sitemap-editor", group="Editor")

    def test_preview_url_is_absent_from_the_sitemap(self):
        from guides.tests.draft_preview_fixtures import publish

        guide = make_draft_guide(
            self.editor, slug="sitemap-guide-en", title="Sitemap Guide"
        )
        publish(guide, self.editor)

        resp = self.client.get("/en/sitemap.xml")
        self.assertEqual(resp.status_code, 200)
        xml = resp.content.decode()
        self.assertNotIn("/preview/", xml)
        self.assertNotIn("/admin/", xml)
        # The ordinary public guide URL is still advertised.
        self.assertIn("/en/guides/sitemap-guide-en/", xml)
