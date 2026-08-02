"""
Beta 11.10 groups D/K: the admin change-form entry point, a never-published
draft previewed by an authorized editor, and the guarantee that the
preview route stays out of every public surface.
"""
from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from compare.tests.draft_preview_fixtures import (
    make_draft_comparison,
    make_user,
    publish,
)


class DraftPreviewButtonTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = make_user("btn-owner", group="Author")
        cls.other_author = make_user("btn-other", group="Author")
        cls.editor = make_user("btn-editor", group="Editor")
        cls.comparison = make_draft_comparison(
            cls.owner, slug="btn-comparison-en", title="Button Comparison"
        )
        cls.comparison.create_translation(
            "de", slug="btn-comparison-de", title="Button Comparison DE",
            intro="i", body="b",
        )

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)

    def _change_url(self):
        return reverse("admin:compare_comparison_change", args=[self.comparison.pk])

    def _expected_link(self, language_code):
        return reverse(
            "admin:compare_comparison_draft_preview", args=[self.comparison.pk, language_code]
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
        resp = self.client.get(reverse("admin:compare_comparison_add"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Preview saved draft")

    def test_button_is_absent_for_a_language_without_a_saved_translation(self):
        english_only = make_draft_comparison(
            self.owner, slug="btn-en-only-en", title="English Only"
        )
        self.client.force_login(self.owner)
        url = reverse("admin:compare_comparison_change", args=[english_only.pk])
        resp = self.client.get(url + "?language=de")
        self.assertNotContains(resp, "Preview saved draft")

    def test_button_is_absent_for_a_user_who_may_not_preview(self):
        # A non-owning Author may open the change form read-only, but must
        # not be offered a preview they are not allowed to open.
        self.client.force_login(self.other_author)
        resp = self.client.get(self._change_url())
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Preview saved draft")


class NeverPublishedDraftPreviewTests(TestCase):
    """Group D: a never-published draft can be previewed by an authorized
    staff member, but stays fully unreachable through every public surface."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.editor = make_user("neverpub-editor", group="Editor")

    def _preview_url(self, pk, language_code="en"):
        return reverse("admin:compare_comparison_draft_preview", args=[pk, language_code])

    def test_authorized_staff_can_preview_a_never_published_draft(self):
        comparison = make_draft_comparison(
            self.editor, slug="neverpub-preview-en", title="Never Published Preview"
        )
        self.client.force_login(self.editor)
        resp = self.client.get(self._preview_url(comparison.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Never Published Preview")

    def test_public_detail_page_404s_for_the_same_draft(self):
        make_draft_comparison(
            self.editor, slug="neverpub-public-en", title="Never Published Public"
        )
        resp = self.client.get("/en/compare/neverpub-public-en/")
        self.assertEqual(resp.status_code, 404)

    def test_never_published_draft_is_absent_from_the_visible_queryset(self):
        from compare.models import Comparison

        comparison = make_draft_comparison(
            self.editor, slug="neverpub-visible-en", title="Search Marker Never Published"
        )
        for obj in Comparison.objects.visible_in_language("en"):
            self.assertNotEqual(obj.pk, comparison.pk)

    def test_never_published_draft_is_absent_from_the_sitemap(self):
        make_draft_comparison(
            self.editor, slug="neverpub-sitemap-en", title="Never Published Sitemap"
        )
        resp = self.client.get("/en/sitemap.xml")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("neverpub-sitemap-en", resp.content.decode())

    def test_never_published_draft_is_absent_from_related_content_of_other_pages(self):
        from core.services import related_comparisons

        never_published = make_draft_comparison(
            self.editor, slug="neverpub-related-en", title="Related Marker Never Published"
        )
        other = make_draft_comparison(
            self.editor, slug="neverpub-related-other-en", title="Related Other"
        )
        other = publish(other, self.editor)

        related = related_comparisons(other, limit=6, language_code="en")
        self.assertNotIn(never_published.pk, [c.pk for c in related])


class PreviewStaysOutOfPublicSurfacesTests(TestCase):
    """Group K: the preview URL must never be advertised publicly."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.editor = make_user("sitemap-editor", group="Editor")

    def test_preview_url_is_absent_from_the_sitemap(self):
        comparison = make_draft_comparison(
            self.editor, slug="sitemap-comparison-en", title="Sitemap Comparison"
        )
        publish(comparison, self.editor)

        resp = self.client.get("/en/sitemap.xml")
        self.assertEqual(resp.status_code, 200)
        xml = resp.content.decode()
        self.assertNotIn("/preview/", xml)
        self.assertNotIn("/admin/", xml)
        # The ordinary public comparison URL is still advertised.
        self.assertIn("/en/compare/sitemap-comparison-en/", xml)

    def test_preview_url_is_absent_from_the_visible_queryset_urls(self):
        """search.adapters.comparisons.ComparisonSearchAdapter and the
        category/card/related projections all build off
        Comparison.objects.visible_in_language(language_code) - the same
        queryset the public list/detail/sitemap surfaces already use - and
        derive each URL via get_absolute_url(), never the admin-namespaced
        preview route."""
        from compare.models import Comparison

        comparison = make_draft_comparison(
            self.editor, slug="search-comparison-en", title="Search Comparison Marker"
        )
        publish(comparison, self.editor)

        for obj in Comparison.objects.visible_in_language("en"):
            with self.subTest(pk=obj.pk):
                url = obj.get_absolute_url()
                self.assertNotIn("/preview/", url)
                self.assertNotIn("/admin/", url)

    def test_live_comparison_remains_correctly_findable_through_search_and_sitemap(self):
        """A quick non-regression: publishing and previewing a comparison
        does not disturb its own public discoverability."""
        from compare.models import Comparison

        comparison = make_draft_comparison(
            self.editor, slug="findable-comparison-en", title="Findable Comparison"
        )
        comparison = publish(comparison, self.editor)

        self.client.force_login(self.editor)
        self.client.get(
            reverse("admin:compare_comparison_draft_preview", args=[comparison.pk, "en"])
        )

        self.assertTrue(
            Comparison.objects.visible_in_language("en").filter(pk=comparison.pk).exists()
        )
        xml = self.client.get("/en/sitemap.xml").content.decode()
        self.assertIn("/en/compare/findable-comparison-en/", xml)
