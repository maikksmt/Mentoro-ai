"""
Beta 11.7 groups E/F: every public use-case surface uses the same live
contract, and a never-published draft appears on none of them.

The surfaces all resolve through ``UseCase.objects.visible_in_language()``
(list, detail, related, latest-content, inventory count, search) or through
the ``published()``-based sitemap, so this module asserts the behaviour they
share rather than re-testing the queryset itself.
"""
from django.conf import settings
from django.core.cache import cache
from django.test import TestCase
from django.utils import translation

from core.services import (
    get_latest_items,
    get_public_inventory,
    related_usecases,
    to_teaser_item,
)
from usecases.models import UseCase
from usecases.tests.live_visibility_fixtures import (
    make_usecase,
    make_user,
    publish,
    save_draft_edit,
    start_review_round,
)

LIVE_TITLE = "Surface Live Title"
DRAFT_TITLE = "Surface Draft Title"
LIVE_SLUG = "surface-live-slug"
DRAFT_SLUG = "surface-draft-slug"


class PublicSurfacesDuringReviewTests(TestCase):
    """Group E: live values everywhere, draft values nowhere."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        cache.clear()
        self.addCleanup(cache.clear)
        self.author = make_user("uc-surface-author")

        self.usecase = make_usecase(
            slug=LIVE_SLUG, title=LIVE_TITLE, intro="Live intro",
            body="Live body", outro="Live outro", author=self.author,
        )
        publish(self.usecase, self.author)
        save_draft_edit(
            self.usecase, "en",
            title=DRAFT_TITLE, intro="Draft intro", body="Draft body",
            outro="Draft outro", slug=DRAFT_SLUG,
        )
        start_review_round(self.usecase, self.author)

    def test_list_page_still_shows_the_use_case_with_live_values(self):
        resp = self.client.get("/en/usecases/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn(LIVE_TITLE, html)
        self.assertNotIn(DRAFT_TITLE, html)

    def test_list_page_links_to_the_live_slug(self):
        html = self.client.get("/en/usecases/").content.decode()
        self.assertIn(f"/en/usecases/{LIVE_SLUG}/", html)
        self.assertNotIn(DRAFT_SLUG, html)

    def test_detail_page_shows_live_values(self):
        html = self.client.get(f"/en/usecases/{LIVE_SLUG}/").content.decode()
        self.assertIn(LIVE_TITLE, html)
        self.assertNotIn(DRAFT_TITLE, html)

    def test_teaser_item_uses_live_values_and_live_slug(self):
        usecase = UseCase.objects.get(pk=self.usecase.pk)
        item = to_teaser_item(usecase, "usecase", language_code="en")
        self.assertEqual(item["title"], LIVE_TITLE)
        self.assertIn(LIVE_SLUG, item["url"])
        self.assertNotIn(DRAFT_SLUG, item["url"])

    def test_latest_content_includes_it_with_live_values(self):
        titles = [i["title"] for i in get_latest_items(limit=20, language_code="en")]
        self.assertIn(LIVE_TITLE, titles)
        self.assertNotIn(DRAFT_TITLE, titles)

    def test_public_inventory_count_includes_it(self):
        self.assertGreaterEqual(get_public_inventory("en")["counts"]["usecases"], 1)

    def test_related_content_lists_it_with_the_live_slug(self):
        other = make_usecase(slug="surface-other", title="Other Live", author=self.author)
        publish(other, self.author)

        related = related_usecases(UseCase.objects.get(pk=other.pk), limit=6, language_code="en")
        items = [to_teaser_item(u, "usecase", language_code="en") for u in related]
        titles = [i["title"] for i in items]
        self.assertIn(LIVE_TITLE, titles)
        self.assertNotIn(DRAFT_TITLE, titles)
        for item in items:
            with self.subTest(url=item["url"]):
                self.assertNotIn(DRAFT_SLUG, item["url"])

    def test_sitemap_advertises_only_the_live_slug(self):
        xml = self.client.get("/en/sitemap.xml").content.decode()
        self.assertNotIn(DRAFT_SLUG, xml)


class NeverPublishedDraftIsInvisibleEverywhereTests(TestCase):
    """Group F: nothing about a never-published use case reaches the public."""

    DRAFT_ONLY_TITLE = "Never Published Marker Title"
    DRAFT_ONLY_SLUG = "never-published-marker-slug"

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        cache.clear()
        self.addCleanup(cache.clear)
        self.author = make_user("uc-neverpub-author")
        self.draft = make_usecase(
            slug=self.DRAFT_ONLY_SLUG, title=self.DRAFT_ONLY_TITLE,
            intro="secret intro", body="secret body", outro="secret outro",
            persona="Secret Persona", author=self.author,
        )
        # A published neighbour so the public surfaces are non-empty.
        self.published = make_usecase(
            slug="neverpub-neighbour", title="Neighbour Live", author=self.author
        )
        publish(self.published, self.author)

    def test_detail_page_404s(self):
        self.assertEqual(
            self.client.get(f"/en/usecases/{self.DRAFT_ONLY_SLUG}/").status_code, 404
        )

    def test_absent_from_the_list_page(self):
        html = self.client.get("/en/usecases/").content.decode()
        self.assertNotIn(self.DRAFT_ONLY_TITLE, html)
        self.assertNotIn(self.DRAFT_ONLY_SLUG, html)

    def test_absent_from_latest_content(self):
        titles = [i["title"] for i in get_latest_items(limit=20, language_code="en")]
        self.assertNotIn(self.DRAFT_ONLY_TITLE, titles)

    def test_absent_from_related_content(self):
        related = related_usecases(
            UseCase.objects.get(pk=self.published.pk), limit=6, language_code="en"
        )
        self.assertNotIn(self.draft.pk, [u.pk for u in related])

    def test_absent_from_the_sitemap(self):
        xml = self.client.get("/en/sitemap.xml").content.decode()
        self.assertNotIn(self.DRAFT_ONLY_SLUG, xml)

    def test_absent_from_the_homepage(self):
        html = self.client.get("/en/").content.decode()
        self.assertNotIn(self.DRAFT_ONLY_TITLE, html)

    def test_absent_from_the_visible_queryset_in_both_languages(self):
        for language in ("en", "de"):
            with self.subTest(language=language):
                self.assertFalse(
                    UseCase.objects.visible_in_language(language)
                    .filter(pk=self.draft.pk)
                    .exists()
                )


class ArchivedUseCaseLeavesPublicSurfacesTests(TestCase):
    """An explicit withdrawal removes it everywhere, snapshot or not."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        cache.clear()
        self.addCleanup(cache.clear)
        self.author = make_user("uc-archived-author")

    def test_archived_usecase_disappears_from_detail_list_and_sitemap(self):
        from usecases.tests.live_visibility_fixtures import archive

        usecase = make_usecase(slug="archived-surface", title="Archived Surface", author=self.author)
        publish(usecase, self.author)
        self.assertEqual(self.client.get("/en/usecases/archived-surface/").status_code, 200)

        archive(usecase, self.author)

        self.assertEqual(self.client.get("/en/usecases/archived-surface/").status_code, 404)
        self.assertNotIn("Archived Surface", self.client.get("/en/usecases/").content.decode())
        self.assertNotIn("archived-surface", self.client.get("/en/sitemap.xml").content.decode())
