"""
Beta 11.8 groups D/J/K: the admin change-form entry point, a never-published
draft previewed by an authorized editor, and the guarantee that the preview
route stays out of every public surface.
"""
from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from usecases.tests.draft_preview_fixtures import make_draft_usecase, make_user, publish


class DraftPreviewButtonTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = make_user("btn-owner", group="Author")
        cls.other_author = make_user("btn-other", group="Author")
        cls.editor = make_user("btn-editor", group="Editor")
        cls.usecase = make_draft_usecase(
            cls.owner, slug="btn-usecase-en", title="Button Usecase"
        )
        cls.usecase.create_translation(
            "de", slug="btn-usecase-de", title="Button Usecase DE",
            intro="i", body="b", outro="o", persona="",
        )

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)

    def _change_url(self):
        return reverse("admin:usecases_usecase_change", args=[self.usecase.pk])

    def _expected_link(self, language_code):
        return reverse(
            "admin:usecases_usecase_draft_preview", args=[self.usecase.pk, language_code]
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
        resp = self.client.get(reverse("admin:usecases_usecase_add"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Preview saved draft")

    def test_button_is_absent_for_a_language_without_a_saved_translation(self):
        english_only = make_draft_usecase(
            self.owner, slug="btn-en-only-en", title="English Only"
        )
        self.client.force_login(self.owner)
        url = reverse("admin:usecases_usecase_change", args=[english_only.pk])
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
            resp, reverse("admin:usecases_usecase_diff", args=[self.usecase.pk])
        )
        self.assertContains(resp, "Draft ↔ Live")


class NeverPublishedDraftPreviewTests(TestCase):
    """Group D: a never-published draft can be previewed by an authorized
    staff member, but stays fully unreachable through every public surface."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.editor = make_user("neverpub-editor", group="Editor")

    def _preview_url(self, pk, language_code="en"):
        return reverse("admin:usecases_usecase_draft_preview", args=[pk, language_code])

    def test_authorized_staff_can_preview_a_never_published_draft(self):
        usecase = make_draft_usecase(
            self.editor, slug="neverpub-preview-en", title="Never Published Preview"
        )
        self.client.force_login(self.editor)
        resp = self.client.get(self._preview_url(usecase.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Never Published Preview")

    def test_public_detail_page_404s_for_the_same_draft(self):
        make_draft_usecase(
            self.editor, slug="neverpub-public-en", title="Never Published Public"
        )
        resp = self.client.get("/en/usecases/neverpub-public-en/")
        self.assertEqual(resp.status_code, 404)

    def test_never_published_draft_is_absent_from_search(self):
        from usecases.models import UseCase

        usecase = make_draft_usecase(
            self.editor, slug="neverpub-search-en", title="Search Marker Never Published"
        )
        for obj in UseCase.objects.visible_in_language("en"):
            self.assertNotEqual(obj.pk, usecase.pk)

    def test_never_published_draft_is_absent_from_the_sitemap(self):
        make_draft_usecase(
            self.editor, slug="neverpub-sitemap-en", title="Never Published Sitemap"
        )
        resp = self.client.get("/en/sitemap.xml")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("neverpub-sitemap-en", resp.content.decode())

    def test_never_published_draft_is_absent_from_related_content_of_other_pages(self):
        from core.services import related_usecases

        never_published = make_draft_usecase(
            self.editor, slug="neverpub-related-en", title="Related Marker Never Published"
        )
        other = make_draft_usecase(
            self.editor, slug="neverpub-related-other-en", title="Related Other"
        )
        other = publish(other, self.editor)

        related = related_usecases(other, limit=6, language_code="en")
        self.assertNotIn(never_published.pk, [u.pk for u in related])


class PreviewStaysOutOfPublicSurfacesTests(TestCase):
    """Group K: the preview URL must never be advertised publicly."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.editor = make_user("sitemap-editor", group="Editor")

    def test_preview_url_is_absent_from_the_sitemap(self):
        usecase = make_draft_usecase(
            self.editor, slug="sitemap-usecase-en", title="Sitemap Usecase"
        )
        publish(usecase, self.editor)

        resp = self.client.get("/en/sitemap.xml")
        self.assertEqual(resp.status_code, 200)
        xml = resp.content.decode()
        self.assertNotIn("/preview/", xml)
        self.assertNotIn("/admin/", xml)
        # The ordinary public usecase URL is still advertised.
        self.assertIn("/en/usecases/sitemap-usecase-en/", xml)

    def test_preview_url_is_absent_from_the_search_visibility_queryset(self):
        """search.adapters.usecases.UseCaseSearchAdapter builds its result set
        from UseCase.objects.visible_in_language(language_code) - the same
        queryset the public list/detail/sitemap surfaces already use - and
        derives each URL via get_absolute_url(), never the admin-namespaced
        preview route."""
        from usecases.models import UseCase

        usecase = make_draft_usecase(
            self.editor, slug="search-usecase-en", title="Search Usecase Marker"
        )
        publish(usecase, self.editor)

        for obj in UseCase.objects.visible_in_language("en"):
            with self.subTest(pk=obj.pk):
                url = obj.get_absolute_url()
                self.assertNotIn("/preview/", url)
                self.assertNotIn("/admin/", url)

    def test_live_usecase_remains_correctly_findable_through_search_and_sitemap(self):
        """A quick non-regression: publishing and previewing a usecase does
        not disturb its own public discoverability."""
        from usecases.models import UseCase

        usecase = make_draft_usecase(
            self.editor, slug="findable-usecase-en", title="Findable Usecase"
        )
        usecase = publish(usecase, self.editor)

        self.client.force_login(self.editor)
        self.client.get(
            reverse("admin:usecases_usecase_draft_preview", args=[usecase.pk, "en"])
        )

        self.assertTrue(
            UseCase.objects.visible_in_language("en").filter(pk=usecase.pk).exists()
        )
        xml = self.client.get("/en/sitemap.xml").content.decode()
        self.assertIn("/en/usecases/findable-usecase-en/", xml)
