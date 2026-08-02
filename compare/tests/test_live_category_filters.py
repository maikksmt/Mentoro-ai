"""
Beta 11.9C: the comparison list's category filter - both the dropdown
options and the actual `?category=` filter - must depend only on each
comparison's published entry snapshot, exactly like the detail page's
``public_tool_entries()`` (Beta 11.9) and the entry-membership boundary
(Beta 11.9B).

Before this slice, both halves read the *current* ``tools`` M2M (through
``ComparisonToolEntry``, i.e. today's draft rows) instead of
``live_entries``:

* ``ComparisonListView.get_queryset()``'s ``category`` filter joined
  ``tools__categories__...`` directly on the visible queryset.
* ``_categories_for_filters()`` built the dropdown from
  ``Category.objects.filter(tools__comparisons__in=ctx["object_list"])`` -
  which is both a draft-relation leak *and* scoped to only the current
  paginated page (``object_list`` is the page's slice, not the full visible
  set - see ``django.views.generic.list.MultipleObjectMixin
  .get_context_data``).

A comparison mid-review with its draft entry swapped to a differently
categorised tool could therefore surface a brand new filter option, or lose
an option it should still offer, purely from an unpublished edit.
"""
from datetime import timedelta

from django.conf import settings
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone, translation

from catalog.models import Category, Tool
from compare.models import Comparison
from compare.tests.live_snapshot_fixtures import (
    add_entry,
    make_comparison,
    make_user,
    publish,
    start_review_round,
)
from compare.views import ComparisonListView

PAST = timezone.now() - timedelta(days=1)
FUTURE = timezone.now() + timedelta(days=30)


def make_category(slug, name=None, language="en"):
    cat = Category.objects.create()
    cat.create_translation(language, name=name or slug, slug=slug)
    return cat


def make_tool(slug, name=None, categories=(), language="en", published_at=None):
    kwargs = {"slug": slug, "website": f"https://example.com/{slug}"}
    if published_at is not None:
        kwargs["published_at"] = published_at
    tool = Tool.objects.create(**kwargs)
    tool.create_translation(language, name=name or slug)
    if categories:
        tool.categories.set(categories)
    return tool


class CategoryFilterTestCase(TestCase):
    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.author = make_user("cmp-catfilter-author")

    def _list_html(self, params=""):
        return self.client.get(f"/en/compare/{params}").content.decode()

    def _option_slugs(self, html):
        """
        The template always renders one placeholder option whose value
        echoes the raw `?category=` query string verbatim ("All
        categories", pre-existing/unrelated to this slice) - excluded here
        by its fixed label so an unknown slug in the URL is never mistaken
        for a real, offered filter option.
        """
        import re
        pairs = re.findall(r'<option value="([^"]*)"[^>]*>\s*([^<]*?)\s*</option>', html)
        return {value for value, text in pairs if text.strip() and text.strip() != "All categories"}

    def _queryset_pks(self, params=None):
        from django.test import RequestFactory
        from django.utils import translation as t
        with t.override("en"):
            request = RequestFactory().get("/en/compare/", params or {})
            view = ComparisonListView()
            view.request = request
            return set(view.get_queryset().values_list("pk", flat=True))


class DraftToolSwapCategoryTests(CategoryFilterTestCase):
    """Group B / Phase 4-A: the reproduction scenario itself."""

    def setUp(self):
        super().setUp()
        self.cat_writing = make_category("cat-writing-swap", "Writing Swap")
        self.cat_video = make_category("cat-video-swap", "Video Swap")
        self.tool_a = make_tool("catswap-tool-a", "CatSwap Tool A", categories=[self.cat_writing], published_at=PAST)
        self.tool_b = make_tool("catswap-tool-b", "CatSwap Tool B", categories=[self.cat_video], published_at=PAST)

        self.comparison = make_comparison(
            slug="catswap-cmp", title="CatSwap Scenario", author=self.author
        )
        self.entry = add_entry(self.comparison, self.tool_a, position=10, summary="Summary A")
        self.published = publish(self.comparison, self.author)

    def test_writing_option_present_before_any_draft_change(self):
        html = self._list_html()
        self.assertIn(self.cat_writing.slug, self._option_slugs(html))

    def test_video_option_absent_before_any_draft_change(self):
        html = self._list_html()
        self.assertNotIn(self.cat_video.slug, self._option_slugs(html))

    def test_filter_writing_finds_comparison_before_draft_change(self):
        self.assertIn(self.comparison.pk, self._queryset_pks({"category": self.cat_writing.slug}))

    def test_filter_video_does_not_find_comparison_before_draft_change(self):
        self.assertNotIn(self.comparison.pk, self._queryset_pks({"category": self.cat_video.slug}))

    def test_after_draft_tool_swap_writing_option_still_present(self):
        """The critical reproduction: swap the draft entry's tool onto a
        differently-categorised tool, without republishing."""
        reviewed = start_review_round(self.published, self.author)
        fresh_entry = reviewed.tool_entries.get(pk=self.entry.pk)
        fresh_entry.tool = self.tool_b
        fresh_entry.save(update_fields=["tool"])

        html = self._list_html()
        options = self._option_slugs(html)
        self.assertIn(self.cat_writing.slug, options)
        self.assertNotIn(self.cat_video.slug, options)

    def test_after_draft_tool_swap_writing_filter_still_matches(self):
        reviewed = start_review_round(self.published, self.author)
        fresh_entry = reviewed.tool_entries.get(pk=self.entry.pk)
        fresh_entry.tool = self.tool_b
        fresh_entry.save(update_fields=["tool"])

        self.assertIn(self.comparison.pk, self._queryset_pks({"category": self.cat_writing.slug}))
        self.assertNotIn(self.comparison.pk, self._queryset_pks({"category": self.cat_video.slug}))

    def test_republish_activates_video_and_deactivates_writing(self):
        reviewed = start_review_round(self.published, self.author)
        fresh_entry = reviewed.tool_entries.get(pk=self.entry.pk)
        fresh_entry.tool = self.tool_b
        fresh_entry.save(update_fields=["tool"])

        in_review = Comparison.objects.get(pk=self.comparison.pk)
        publish(in_review, self.author)

        html = self._list_html()
        options = self._option_slugs(html)
        self.assertIn(self.cat_video.slug, options)
        self.assertNotIn(self.cat_writing.slug, options)
        self.assertIn(self.comparison.pk, self._queryset_pks({"category": self.cat_video.slug}))
        self.assertNotIn(self.comparison.pk, self._queryset_pks({"category": self.cat_writing.slug}))


class NewDraftEntryCategoryTests(CategoryFilterTestCase):
    """Group C / Phase 4-B: a brand new draft entry must not surface its
    tool's category at all before republish."""

    def setUp(self):
        super().setUp()
        self.cat_writing = make_category("cat-writing-newentry", "Writing NewEntry")
        self.cat_video = make_category("cat-video-newentry", "Video NewEntry")
        self.tool_a = make_tool("catnew-tool-a", "CatNew Tool A", categories=[self.cat_writing], published_at=PAST)
        self.tool_b = make_tool("catnew-tool-b", "CatNew Tool B", categories=[self.cat_video], published_at=PAST)

        self.comparison = make_comparison(
            slug="catnew-cmp", title="CatNew Scenario", author=self.author
        )
        add_entry(self.comparison, self.tool_a, position=10, summary="Summary A")
        self.published = publish(self.comparison, self.author)

        reviewed = start_review_round(self.published, self.author)
        add_entry(reviewed, self.tool_b, position=20, summary="Summary B")

    def test_video_option_absent_for_new_draft_entry(self):
        html = self._list_html()
        self.assertNotIn(self.cat_video.slug, self._option_slugs(html))

    def test_filter_video_does_not_find_comparison(self):
        self.assertNotIn(self.comparison.pk, self._queryset_pks({"category": self.cat_video.slug}))

    def test_republish_activates_video(self):
        in_review = Comparison.objects.get(pk=self.comparison.pk)
        publish(in_review, self.author)
        html = self._list_html()
        self.assertIn(self.cat_video.slug, self._option_slugs(html))
        self.assertIn(self.comparison.pk, self._queryset_pks({"category": self.cat_video.slug}))


class DraftDeletionCategoryTests(CategoryFilterTestCase):
    """Group D / Phase 4-C: deleting the draft row must not remove the
    live category before republish."""

    def setUp(self):
        super().setUp()
        self.cat_writing = make_category("cat-writing-del", "Writing Del")
        self.tool_a = make_tool("catdel-tool-a", "CatDel Tool A", categories=[self.cat_writing], published_at=PAST)

        self.comparison = make_comparison(
            slug="catdel-cmp", title="CatDel Scenario", author=self.author
        )
        self.entry = add_entry(self.comparison, self.tool_a, position=10, summary="Summary A")
        self.published = publish(self.comparison, self.author)

        reviewed = start_review_round(self.published, self.author)
        reviewed.tool_entries.get(pk=self.entry.pk).delete()

    def test_writing_option_still_present(self):
        html = self._list_html()
        self.assertIn(self.cat_writing.slug, self._option_slugs(html))

    def test_filter_writing_still_matches(self):
        self.assertIn(self.comparison.pk, self._queryset_pks({"category": self.cat_writing.slug}))

    def test_republish_removes_writing(self):
        in_review = Comparison.objects.get(pk=self.comparison.pk)
        publish(in_review, self.author)
        html = self._list_html()
        self.assertNotIn(self.cat_writing.slug, self._option_slugs(html))
        self.assertNotIn(self.comparison.pk, self._queryset_pks({"category": self.cat_writing.slug}))


class DraftReorderCategoryTests(CategoryFilterTestCase):
    """Group E: reordering draft positions must not change options, results
    or counts."""

    def setUp(self):
        super().setUp()
        self.cat_writing = make_category("cat-writing-order", "Writing Order")
        self.cat_video = make_category("cat-video-order", "Video Order")
        self.tool_a = make_tool("catorder-tool-a", "CatOrder Tool A", categories=[self.cat_writing], published_at=PAST)
        self.tool_b = make_tool("catorder-tool-b", "CatOrder Tool B", categories=[self.cat_video], published_at=PAST)

        self.comparison = make_comparison(
            slug="catorder-cmp", title="CatOrder Scenario", author=self.author
        )
        self.entry_a = add_entry(self.comparison, self.tool_a, position=10, summary="Summary A")
        self.entry_b = add_entry(self.comparison, self.tool_b, position=20, summary="Summary B")
        self.published = publish(self.comparison, self.author)

    def test_reordering_draft_positions_does_not_change_options_or_counts(self):
        before_html = self._list_html()
        before_options = self._option_slugs(before_html)
        before_count = self._queryset_pks().__len__()

        reviewed = start_review_round(self.published, self.author)
        reviewed.tool_entries.filter(pk=self.entry_a.pk).update(position=99)
        reviewed.tool_entries.filter(pk=self.entry_b.pk).update(position=5)

        after_html = self._list_html()
        after_options = self._option_slugs(after_html)
        after_count = self._queryset_pks().__len__()

        self.assertEqual(before_options, after_options)
        self.assertEqual(before_count, after_count)


class GlobalToolVisibilityCategoryTests(CategoryFilterTestCase):
    """Group F: global tool state changes reach the category filter without
    a comparison republish - the counterpart to the membership contract."""

    def setUp(self):
        super().setUp()
        self.cat_writing = make_category("cat-writing-global", "Writing Global")
        self.tool_a = make_tool("catglobal-tool-a", "CatGlobal Tool A", categories=[self.cat_writing], published_at=FUTURE)

        self.comparison = make_comparison(
            slug="catglobal-cmp", title="CatGlobal Scenario", author=self.author
        )
        add_entry(self.comparison, self.tool_a, position=10, summary="Summary A")
        self.published = publish(self.comparison, self.author)

    def test_future_tool_contributes_no_category(self):
        html = self._list_html()
        self.assertNotIn(self.cat_writing.slug, self._option_slugs(html))
        self.assertNotIn(self.comparison.pk, self._queryset_pks({"category": self.cat_writing.slug}))

    def test_tool_becoming_public_activates_category_without_republish(self):
        Tool.objects.filter(pk=self.tool_a.pk).update(published_at=PAST)
        html = self._list_html()
        self.assertIn(self.cat_writing.slug, self._option_slugs(html))
        self.assertIn(self.comparison.pk, self._queryset_pks({"category": self.cat_writing.slug}))

    def test_tool_withdrawn_again_removes_category(self):
        Tool.objects.filter(pk=self.tool_a.pk).update(published_at=PAST)
        Tool.objects.filter(pk=self.tool_a.pk).update(published_at=FUTURE)
        html = self._list_html()
        self.assertNotIn(self.cat_writing.slug, self._option_slugs(html))

    def test_deleted_tool_contributes_no_category_and_page_still_works(self):
        Tool.objects.filter(pk=self.tool_a.pk).update(published_at=PAST)
        self.tool_a.delete()
        resp = self.client.get("/en/compare/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(self.cat_writing.slug, self._option_slugs(resp.content.decode()))


class SnapshotMembershipCategoryTests(CategoryFilterTestCase):
    """Group G: a globally public tool that is only a *draft* entry
    contributes nothing, in contrast to one that is a genuine snapshot
    member."""

    def setUp(self):
        super().setUp()
        self.cat_video = make_category("cat-video-membership", "Video Membership")
        self.tool_a = make_tool("catmemb-tool-a", "CatMemb Tool A", published_at=PAST)
        self.tool_b = make_tool("catmemb-tool-b", "CatMemb Tool B", categories=[self.cat_video], published_at=PAST)

        self.comparison = make_comparison(
            slug="catmemb-cmp", title="CatMemb Scenario", author=self.author
        )
        add_entry(self.comparison, self.tool_a, position=10, summary="Summary A")
        self.published = publish(self.comparison, self.author)

        reviewed = start_review_round(self.published, self.author)
        add_entry(reviewed, self.tool_b, position=20, summary="Summary B")

    def test_draft_only_tool_contributes_nothing(self):
        html = self._list_html()
        self.assertNotIn(self.cat_video.slug, self._option_slugs(html))
        self.assertNotIn(self.comparison.pk, self._queryset_pks({"category": self.cat_video.slug}))

    def test_snapshot_member_tool_does_contribute(self):
        """Positive counterpart, mirroring GlobalToolVisibilityCategoryTests
        but for a tool that WAS in the snapshot from the start."""
        cat_writing = make_category("cat-writing-membership2", "Writing Membership 2")
        tool_c = make_tool(
            "catmemb-tool-c", "CatMemb Tool C", categories=[cat_writing], published_at=FUTURE
        )
        comparison2 = make_comparison(
            slug="catmemb-cmp2", title="CatMemb Scenario 2", author=self.author
        )
        add_entry(comparison2, tool_c, position=10, summary="Summary C")
        published2 = publish(comparison2, self.author)

        self.assertNotIn(cat_writing.slug, self._option_slugs(self._list_html()))
        Tool.objects.filter(pk=tool_c.pk).update(published_at=PAST)
        html = self._list_html()
        self.assertIn(cat_writing.slug, self._option_slugs(html))
        self.assertIn(published2.pk, self._queryset_pks({"category": cat_writing.slug}))


class LanguageIsolationCategoryTests(CategoryFilterTestCase):
    """Group H: category contribution follows the comparison's own public
    language state, with no cross-language fallback."""

    def setUp(self):
        super().setUp()
        self.cat_writing = make_category("cat-writing-lang", "Writing Lang", language="en")
        self.cat_writing.create_translation("de", name="Schreiben Lang", slug="cat-writing-lang-de")
        self.tool_a = make_tool("catlang-tool-a", "CatLang Tool A", categories=[self.cat_writing], published_at=PAST)

        self.comparison = make_comparison(
            slug="catlang-cmp-en", title="CatLang Scenario EN", author=self.author
        )
        add_entry(self.comparison, self.tool_a, position=10, summary="Summary A")
        self.published = publish(self.comparison, self.author)

    def test_en_live_comparison_contributes_to_en_list_only(self):
        html_en = self._list_html()
        self.assertIn(self.cat_writing.slug, self._option_slugs(html_en))

        html_de = self.client.get("/de/compare/").content.decode()
        self.assertNotIn(self.cat_writing.slug, self._option_slugs(html_de))
        self.assertNotIn("cat-writing-lang-de", self._option_slugs(html_de))

    def test_de_translation_only_draft_does_not_activate_de(self):
        reviewed = start_review_round(self.published, self.author)
        reviewed.create_translation(
            "de", title="CatLang Scenario DE", intro="i", body="b", slug="catlang-cmp-de"
        )
        html_de = self.client.get("/de/compare/").content.decode()
        self.assertNotIn("cat-writing-lang-de", self._option_slugs(html_de))


class StateCLegacyCategoryTests(CategoryFilterTestCase):
    """Group I: legacy published records (live_entries is None) keep the
    documented legacy category contribution while strictly published, and
    lose it the moment they leave that status."""

    def setUp(self):
        super().setUp()
        self.cat_writing = make_category("cat-writing-legacy", "Writing Legacy")
        self.tool_a = make_tool("catlegacy-tool-a", "CatLegacy Tool A", categories=[self.cat_writing], published_at=PAST)
        self.comparison = Comparison.objects.create(status="published", published_at=timezone.now())
        self.comparison.create_translation(
            "en", title="CatLegacy Scenario", intro="i", body="b", slug="catlegacy-cmp"
        )
        self.comparison.tool_entries.create(tool=self.tool_a, position=10).create_translation(
            "en", label="", summary="Legacy summary", pros="", cons="", special=""
        )

    def test_legacy_published_record_contributes_its_category(self):
        self.assertIsNone(self.comparison.live_entries)
        html = self._list_html()
        self.assertIn(self.cat_writing.slug, self._option_slugs(html))
        self.assertIn(self.comparison.pk, self._queryset_pks({"category": self.cat_writing.slug}))

    def test_legacy_record_in_review_is_invisible_and_contributes_nothing(self):
        fresh = Comparison.objects.get(pk=self.comparison.pk)
        fresh.move_to_review(by=self.author)
        fresh.save()

        html = self._list_html()
        self.assertNotIn(self.cat_writing.slug, self._option_slugs(html))
        self.assertNotIn(self.comparison.pk, self._queryset_pks({"category": self.cat_writing.slug}))

    def test_empty_snapshot_list_is_not_a_legacy_fallback(self):
        Comparison.objects.filter(pk=self.comparison.pk).update(live_entries=[])
        html = self._list_html()
        self.assertNotIn(self.cat_writing.slug, self._option_slugs(html))
        self.assertNotIn(self.comparison.pk, self._queryset_pks({"category": self.cat_writing.slug}))


class DeduplicationAndSortingTests(CategoryFilterTestCase):
    def test_category_appears_once_despite_multiple_comparisons_and_tools(self):
        cat = make_category("cat-dedup", "Dedup Cat")
        tool_1 = make_tool("catdedup-tool-1", "CatDedup Tool 1", categories=[cat], published_at=PAST)
        tool_2 = make_tool("catdedup-tool-2", "CatDedup Tool 2", categories=[cat], published_at=PAST)

        c1 = make_comparison(slug="catdedup-cmp1", title="CatDedup 1", author=self.author)
        add_entry(c1, tool_1, position=10, summary="S1")
        publish(c1, self.author)

        c2 = make_comparison(slug="catdedup-cmp2", title="CatDedup 2", author=self.author)
        add_entry(c2, tool_2, position=10, summary="S2")
        publish(c2, self.author)

        html = self._list_html()
        self.assertEqual(html.count(f'value="{cat.slug}"'), 1)

    def test_no_duplicate_comparisons_in_filter_result(self):
        cat = make_category("cat-nodupe", "NoDupe Cat")
        tool_1 = make_tool("catnodupe-tool-1", "CatNoDupe Tool 1", categories=[cat], published_at=PAST)
        tool_2 = make_tool("catnodupe-tool-2", "CatNoDupe Tool 2", categories=[cat], published_at=PAST)

        c1 = make_comparison(slug="catnodupe-cmp1", title="CatNoDupe 1", author=self.author)
        add_entry(c1, tool_1, position=10, summary="S1")
        add_entry(c1, tool_2, position=20, summary="S2")
        publish(c1, self.author)

        pks = list(self._queryset_pks({"category": cat.slug}))
        self.assertEqual(len(pks), len(set(pks)))


class InvalidCategoryFilterTests(CategoryFilterTestCase):
    def test_unknown_slug_returns_no_matches_without_error(self):
        resp = self.client.get("/en/compare/?category=totally-unknown-slug")
        self.assertEqual(resp.status_code, 200)

    def test_unknown_slug_does_not_add_an_option(self):
        html = self._list_html("?category=totally-unknown-slug")
        self.assertNotIn("totally-unknown-slug", self._option_slugs(html))


class QueryCountTests(CategoryFilterTestCase):
    """
    Isolates the category-filter machinery itself (get_queryset() +
    _categories_for_filters()) from unrelated, pre-existing per-card
    rendering overhead (author lookups, parler's DB-backed translation
    cache for each tool badge - both present before this slice and
    unaffected by it). Measuring the full templated page instead would
    make this test depend on that unrelated overhead rather than on what
    Beta 11.9C actually controls.
    """

    def _make_published_comparisons(self, cat, n, prefix):
        for i in range(n):
            tool = make_tool(f"{prefix}-tool-{i}", f"{prefix} Tool {i}", categories=[cat], published_at=PAST)
            c = make_comparison(slug=f"{prefix}-cmp-{i}", title=f"{prefix} {i}", author=self.author)
            add_entry(c, tool, position=10, summary=f"S{i}")
            publish(c, self.author)

    def _filter_machinery_query_count(self, cat_slug):
        from django.test import RequestFactory
        request = RequestFactory().get("/en/compare/", {"category": cat_slug})
        view = ComparisonListView()
        view.request = request
        with CaptureQueriesContext(connection) as captured:
            list(view.get_queryset())
            list(view._categories_for_filters())
        return len(captured.captured_queries)

    def test_query_count_stays_constant_as_comparisons_grow(self):
        cat_small = make_category("cat-qsmall", "QSmall Cat")
        self._make_published_comparisons(cat_small, 3, "qsmall")
        small_count = self._filter_machinery_query_count(cat_small.slug)

        cat_large = make_category("cat-qlarge", "QLarge Cat")
        self._make_published_comparisons(cat_large, 15, "qlarge")
        large_count = self._filter_machinery_query_count(cat_large.slug)

        self.assertEqual(
            small_count, large_count,
            "filter machinery query count must not scale with comparison count",
        )
        self.assertLessEqual(small_count, 8)

    def test_full_page_response_still_succeeds_with_many_comparisons(self):
        cat = make_category("cat-qcount", "QCount Cat")
        self._make_published_comparisons(cat, 8, "qcount")
        resp = self.client.get("/en/compare/?category=" + cat.slug)
        self.assertEqual(resp.status_code, 200)


class DataIntegrityTests(CategoryFilterTestCase):
    def test_list_get_does_not_mutate_anything(self):
        cat = make_category("cat-integrity", "Integrity Cat")
        tool = make_tool("catintegrity-tool", "CatIntegrity Tool", categories=[cat], published_at=PAST)
        comparison = make_comparison(slug="catintegrity-cmp", title="Integrity Scenario", author=self.author)
        add_entry(comparison, tool, position=10, summary="Summary")
        publish(comparison, self.author)

        before = Comparison.objects.get(pk=comparison.pk)
        before_snapshot = before.live_entries
        before_i18n = before.live_i18n
        before_status = before.status
        before_updated = before.updated_at
        before_draft = list(before.tool_entries.values_list("pk", "position", "tool_id"))

        for _ in range(3):
            self.client.get("/en/compare/?category=" + cat.slug)

        after = Comparison.objects.get(pk=comparison.pk)
        self.assertEqual(after.live_entries, before_snapshot)
        self.assertEqual(after.live_i18n, before_i18n)
        self.assertEqual(after.status, before_status)
        self.assertEqual(after.updated_at, before_updated)
        self.assertEqual(list(after.tool_entries.values_list("pk", "position", "tool_id")), before_draft)
