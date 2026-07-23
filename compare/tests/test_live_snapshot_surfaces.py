"""
Beta 11.9 groups F/G/J: every public comparison surface uses the same live
contract, and reading one changes nothing.

The surfaces all resolve through ``Comparison.objects.visible_in_language()``
(list, detail, related, search, and - since this slice - the sitemap), so
this module asserts the behaviour they share rather than re-testing the
queryset itself.
"""
from django.conf import settings
from django.core.cache import cache
from django.test import TestCase
from django.utils import translation
from reversion.models import Version

from compare.models import Comparison, ComparisonToolEntry
from compare.tests.live_snapshot_fixtures import (
    add_entry,
    archive,
    make_comparison,
    make_tool,
    make_user,
    publish,
    save_draft_edit,
    save_entry_draft_edit,
    start_review_round,
)
from core.services import related_comparisons, to_teaser_item

LIVE_TITLE = "Surface Live Title"
DRAFT_TITLE = "Surface Draft Title"
LIVE_SLUG = "surface-live-slug"
DRAFT_SLUG = "surface-draft-slug"


class PublicSurfacesDuringReviewTests(TestCase):
    """Groups F/G: live values everywhere, draft values nowhere."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        cache.clear()
        self.addCleanup(cache.clear)
        self.author = make_user("cmp-surface-author")
        self.tool = make_tool("surface-tool", "Surface Tool")

        self.comparison = make_comparison(
            slug=LIVE_SLUG, title=LIVE_TITLE, intro="Live intro", body="Live body",
            author=self.author,
        )
        self.entry = add_entry(
            self.comparison, self.tool, position=10, summary="<p>Live entry summary</p>"
        )
        publish(self.comparison, self.author)

        save_draft_edit(
            self.comparison, "en",
            title=DRAFT_TITLE, intro="Draft intro", body="Draft body", slug=DRAFT_SLUG,
        )
        save_entry_draft_edit(self.entry, "en", summary="<p>Draft entry summary</p>")
        start_review_round(self.comparison, self.author)

    def test_live_slug_returns_200_during_review(self):
        self.assertEqual(self.client.get(f"/en/compare/{LIVE_SLUG}/").status_code, 200)

    def test_draft_slug_still_404s(self):
        self.assertEqual(self.client.get(f"/en/compare/{DRAFT_SLUG}/").status_code, 404)

    def test_list_page_shows_live_values_and_links_to_the_live_slug(self):
        html = self.client.get("/en/compare/").content.decode()
        self.assertIn(LIVE_TITLE, html)
        self.assertNotIn(DRAFT_TITLE, html)
        self.assertIn(f"/en/compare/{LIVE_SLUG}/", html)
        self.assertNotIn(DRAFT_SLUG, html)

    def test_canonical_uses_the_live_slug(self):
        canonical = self.client.get(f"/en/compare/{LIVE_SLUG}/").context["seo"].canonical
        self.assertIn(LIVE_SLUG, canonical)
        self.assertNotIn(DRAFT_SLUG, canonical)

    def test_sitemap_advertises_the_live_slug_only(self):
        xml = self.client.get("/en/sitemap.xml").content.decode()
        self.assertIn(f"/en/compare/{LIVE_SLUG}/", xml)
        self.assertNotIn(DRAFT_SLUG, xml)

    def test_teaser_uses_live_values_and_the_live_slug(self):
        comparison = Comparison.objects.get(pk=self.comparison.pk)
        item = to_teaser_item(comparison, "comparison", language_code="en")
        self.assertEqual(item["title"], LIVE_TITLE)
        self.assertIn(LIVE_SLUG, item["url"])
        self.assertNotIn(DRAFT_SLUG, item["url"])

    def test_related_content_of_another_comparison_uses_live_values(self):
        other = make_comparison(
            slug="surface-other", title="Surface Other", author=self.author
        )
        add_entry(other, self.tool, position=10, summary="Other summary")
        other = publish(other, self.author)

        related = related_comparisons(other, limit=6, language_code="en")
        items = [to_teaser_item(c, "comparison", language_code="en") for c in related]
        titles = [i["title"] for i in items]
        self.assertIn(LIVE_TITLE, titles)
        self.assertNotIn(DRAFT_TITLE, titles)
        for item in items:
            with self.subTest(url=item["url"]):
                self.assertNotIn(DRAFT_SLUG, item["url"])

    def test_detail_page_shows_the_live_entry_summary(self):
        html = self.client.get(f"/en/compare/{LIVE_SLUG}/").content.decode()
        self.assertIn("Live entry summary", html)
        self.assertNotIn("Draft entry summary", html)


class NeverPublishedComparisonIsInvisibleEverywhereTests(TestCase):
    """A never-published comparison reaches no public surface at all."""

    DRAFT_ONLY_TITLE = "Never Published Marker Title"
    DRAFT_ONLY_SLUG = "never-published-marker-slug"

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        cache.clear()
        self.addCleanup(cache.clear)
        self.author = make_user("cmp-neverpub-author")
        self.tool = make_tool("neverpub-tool", "NeverPub Tool")

        self.draft = make_comparison(
            slug=self.DRAFT_ONLY_SLUG, title=self.DRAFT_ONLY_TITLE,
            intro="secret intro", body="secret body", author=self.author,
        )
        add_entry(self.draft, self.tool, position=10, summary="secret entry summary")

        self.published = make_comparison(
            slug="neverpub-neighbour", title="Neighbour Live", author=self.author
        )
        add_entry(self.published, self.tool, position=10, summary="Neighbour summary")
        self.published = publish(self.published, self.author)

    def test_detail_page_404s(self):
        self.assertEqual(
            self.client.get(f"/en/compare/{self.DRAFT_ONLY_SLUG}/").status_code, 404
        )

    def test_absent_from_the_list_page(self):
        html = self.client.get("/en/compare/").content.decode()
        self.assertNotIn(self.DRAFT_ONLY_TITLE, html)
        self.assertNotIn(self.DRAFT_ONLY_SLUG, html)

    def test_absent_from_the_sitemap(self):
        xml = self.client.get("/en/sitemap.xml").content.decode()
        self.assertNotIn(self.DRAFT_ONLY_SLUG, xml)

    def test_absent_from_related_content(self):
        related = related_comparisons(
            Comparison.objects.get(pk=self.published.pk), limit=6, language_code="en"
        )
        self.assertNotIn(self.draft.pk, [c.pk for c in related])

    def test_absent_from_the_visible_queryset_in_both_languages(self):
        for language in ("en", "de"):
            with self.subTest(language=language):
                self.assertFalse(
                    Comparison.objects.visible_in_language(language)
                    .filter(pk=self.draft.pk)
                    .exists()
                )


class ArchivedComparisonLeavesPublicSurfacesTests(TestCase):
    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        cache.clear()
        self.addCleanup(cache.clear)
        self.author = make_user("cmp-archived-author")
        self.tool = make_tool("archived-tool", "Archived Tool")

    def test_archived_comparison_disappears_from_detail_list_and_sitemap(self):
        comparison = make_comparison(
            slug="archived-surface", title="Archived Surface", author=self.author
        )
        add_entry(comparison, self.tool, position=10, summary="Archived summary")
        published = publish(comparison, self.author)
        self.assertEqual(self.client.get("/en/compare/archived-surface/").status_code, 200)

        archive(published, self.author)

        self.assertEqual(self.client.get("/en/compare/archived-surface/").status_code, 404)
        self.assertNotIn("Archived Surface", self.client.get("/en/compare/").content.decode())
        self.assertNotIn(
            "archived-surface", self.client.get("/en/sitemap.xml").content.decode()
        )


class CacheAndDataIntegrityTests(TestCase):
    """Group J: public reads are side-effect free and cache-safe."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        cache.clear()
        self.addCleanup(cache.clear)
        self.author = make_user("cmp-integrity-author")
        self.tool = make_tool("integrity-tool", "Integrity Tool")

        self.comparison = make_comparison(
            slug="integrity-slug", title="Integrity Live", author=self.author
        )
        self.entry = add_entry(
            self.comparison, self.tool, position=10, summary="<p>Integrity live summary</p>"
        )
        publish(self.comparison, self.author)
        save_draft_edit(self.comparison, "en", title="Integrity Draft")
        save_entry_draft_edit(self.entry, "en", summary="<p>Integrity draft summary</p>")
        start_review_round(self.comparison, self.author)

    def _state(self):
        comparison = Comparison.objects.get(pk=self.comparison.pk)
        entry = ComparisonToolEntry.objects.get(pk=self.entry.pk)
        return {
            "status": comparison.status,
            "is_published": comparison.is_published,
            "live_i18n": comparison.live_i18n,
            "live_entries": comparison.live_entries,
            "last_published_revision_id": comparison.last_published_revision_id,
            "published_at": comparison.published_at,
            "updated_at": comparison.updated_at,
            "reviewed_at": comparison.reviewed_at,
            "reviewed_by_id": comparison.reviewed_by_id,
            "draft_title": comparison.safe_translation_getter("title", language_code="en"),
            "entry_position": entry.position,
            "entry_tool_id": entry.tool_id,
            "entry_summary": entry.safe_translation_getter("summary", language_code="en"),
            "entry_count": comparison.tool_entries.count(),
            "revision_count": Version.objects.count(),
        }

    def test_public_gets_change_no_persisted_state(self):
        before = self._state()
        for _ in range(3):
            self.client.get("/en/compare/integrity-slug/")
            self.client.get("/en/compare/")
            self.client.get("/en/sitemap.xml")
        self.assertEqual(before, self._state())

    def test_public_stays_on_live_values_across_a_cache_clear(self):
        html = self.client.get("/en/compare/integrity-slug/").content.decode()
        self.assertIn("Integrity Live", html)
        self.assertIn("Integrity live summary", html)

        cache.clear()

        html = self.client.get("/en/compare/integrity-slug/").content.decode()
        self.assertIn("Integrity Live", html)
        self.assertIn("Integrity live summary", html)
        self.assertNotIn("Integrity Draft", html)
        self.assertNotIn("Integrity draft summary", html)

    def test_public_inventory_cache_holds_no_draft_value(self):
        from core.services import get_public_inventory

        self.client.get("/en/compare/integrity-slug/")
        get_public_inventory("en")
        for language in ("en", "de"):
            key = f"mentoroai:public-inventory:v5:{language}"
            with self.subTest(language=language):
                cached = repr(cache.get(key))
                self.assertNotIn("Integrity Draft", cached)
                self.assertNotIn("Integrity draft summary", cached)
