"""
Beta 11.9D: the comparison list card's tool badges.

Beta 11.9C closed the category filter's draft-relation leak but left the
card itself untouched: ``templates/compare/comparison_list.html`` still
rendered ``tools=obj.tools.all`` - the *current* M2M through
``ComparisonToolEntry``, i.e. today's draft rows. A draft tool swap, a new
draft entry, or a draft-row deletion could therefore change a public card's
badge and link before republish, even though the detail page (Beta 11.9),
the category filter (Beta 11.9C) and JSON-LD already stayed on the last
published entry snapshot.

This module pins the fix: card badges come from
``compare.presentation.public_tools_for_comparisons()``, the same
State-A/State-C boundary and the same ``Tool.objects.public()`` contract
every other public Comparison surface already uses.
"""
from datetime import timedelta

from django.conf import settings
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone, translation

from catalog.models import Tool
from compare.models import Comparison
from compare.presentation import public_tools_for_comparisons
from compare.tests.live_snapshot_fixtures import (
    add_entry,
    make_comparison,
    make_tool,
    make_user,
    publish,
    start_review_round,
)

PAST = timezone.now() - timedelta(days=1)
FUTURE = timezone.now() + timedelta(days=30)


class CardToolTestCase(TestCase):
    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.author = make_user("cmp-cardtool-author")

    def _tool_url(self, slug):
        return reverse("catalog:detail", kwargs={"slug": slug})


class LiveBadgeTests(CardToolTestCase):
    """Group A."""

    def setUp(self):
        super().setUp()
        self.tool = make_tool("cardlive-tool-a", "CardLive Tool A", published_at=PAST)
        self.comparison = make_comparison(
            slug="cardlive-cmp", title="CardLive Scenario", author=self.author
        )
        add_entry(self.comparison, self.tool, position=10, summary="Summary A")
        self.published = publish(self.comparison, self.author)

    def test_card_shows_the_live_tool(self):
        html = self.client.get("/en/compare/").content.decode()
        self.assertIn("CardLive Tool A", html)
        self.assertIn(self._tool_url(self.tool.slug), html)

    def test_no_admin_or_preview_url_on_card(self):
        html = self.client.get("/en/compare/").content.decode()
        self.assertNotIn("/admin/", html)
        self.assertNotIn("/preview/", html)

    def test_projection_returns_the_tool_in_order(self):
        tools_by_pk = public_tools_for_comparisons([Comparison.objects.get(pk=self.comparison.pk)])
        self.assertEqual([t.pk for t in tools_by_pk[self.comparison.pk]], [self.tool.pk])


class DraftToolSwapCardTests(CardToolTestCase):
    """Group B / Phase 4-A: the reproduction scenario itself."""

    def setUp(self):
        super().setUp()
        self.tool_a = make_tool("cardswap-tool-a", "CardSwap Tool A", published_at=PAST)
        self.tool_b = make_tool("cardswap-tool-b", "CardSwap Tool B", published_at=PAST)
        self.comparison = make_comparison(
            slug="cardswap-cmp", title="CardSwap Scenario", author=self.author
        )
        self.entry = add_entry(self.comparison, self.tool_a, position=10, summary="Summary A")
        self.published = publish(self.comparison, self.author)

    def _swap_draft_to_b(self):
        reviewed = start_review_round(self.published, self.author)
        fresh_entry = reviewed.tool_entries.get(pk=self.entry.pk)
        fresh_entry.tool = self.tool_b
        fresh_entry.save(update_fields=["tool"])

    def test_card_shows_a_before_any_draft_change(self):
        html = self.client.get("/en/compare/").content.decode()
        self.assertIn("CardSwap Tool A", html)

    def test_card_still_shows_a_after_draft_swap_to_b(self):
        self._swap_draft_to_b()
        html = self.client.get("/en/compare/").content.decode()
        self.assertIn("CardSwap Tool A", html)
        self.assertNotIn("CardSwap Tool B", html)
        self.assertIn(self._tool_url(self.tool_a.slug), html)
        self.assertNotIn(self._tool_url(self.tool_b.slug), html)

    def test_detail_and_card_agree_after_draft_swap(self):
        self._swap_draft_to_b()
        detail_html = self.client.get("/en/compare/cardswap-cmp/").content.decode()
        list_html = self.client.get("/en/compare/").content.decode()
        self.assertIn("CardSwap Tool A", detail_html)
        self.assertIn("CardSwap Tool A", list_html)
        self.assertNotIn("CardSwap Tool B", detail_html)
        self.assertNotIn("CardSwap Tool B", list_html)

    def test_republish_activates_b_on_the_card(self):
        self._swap_draft_to_b()
        in_review = Comparison.objects.get(pk=self.comparison.pk)
        publish(in_review, self.author)
        html = self.client.get("/en/compare/").content.decode()
        self.assertIn("CardSwap Tool B", html)
        self.assertNotIn("CardSwap Tool A", html)


class NewDraftEntryCardTests(CardToolTestCase):
    """Group C."""

    def setUp(self):
        super().setUp()
        self.tool_a = make_tool("cardnew-tool-a", "CardNew Tool A", published_at=PAST)
        self.tool_b = make_tool("cardnew-tool-b", "CardNew Tool B", published_at=PAST)
        self.comparison = make_comparison(
            slug="cardnew-cmp", title="CardNew Scenario", author=self.author
        )
        add_entry(self.comparison, self.tool_a, position=10, summary="Summary A")
        self.published = publish(self.comparison, self.author)

        reviewed = start_review_round(self.published, self.author)
        add_entry(reviewed, self.tool_b, position=20, summary="Summary B")

    def test_card_shows_only_a_before_republish(self):
        html = self.client.get("/en/compare/").content.decode()
        self.assertIn("CardNew Tool A", html)
        self.assertNotIn("CardNew Tool B", html)

    def test_republish_adds_b_in_snapshot_order(self):
        in_review = Comparison.objects.get(pk=self.comparison.pk)
        publish(in_review, self.author)
        html = self.client.get("/en/compare/").content.decode()
        pos_a = html.find("CardNew Tool A")
        pos_b = html.find("CardNew Tool B")
        self.assertNotEqual(pos_a, -1)
        self.assertNotEqual(pos_b, -1)
        self.assertLess(pos_a, pos_b)


class DraftDeletionCardTests(CardToolTestCase):
    """Group D."""

    def setUp(self):
        super().setUp()
        self.tool_a = make_tool("carddel-tool-a", "CardDel Tool A", published_at=PAST)
        self.comparison = make_comparison(
            slug="carddel-cmp", title="CardDel Scenario", author=self.author
        )
        self.entry = add_entry(self.comparison, self.tool_a, position=10, summary="Summary A")
        self.published = publish(self.comparison, self.author)

        reviewed = start_review_round(self.published, self.author)
        reviewed.tool_entries.get(pk=self.entry.pk).delete()

    def test_card_still_shows_a_before_republish(self):
        html = self.client.get("/en/compare/").content.decode()
        self.assertIn("CardDel Tool A", html)

    def test_republish_removes_the_badge(self):
        in_review = Comparison.objects.get(pk=self.comparison.pk)
        publish(in_review, self.author)
        html = self.client.get("/en/compare/").content.decode()
        self.assertNotIn("CardDel Tool A", html)


class DraftReorderCardTests(CardToolTestCase):
    """Group E."""

    def setUp(self):
        super().setUp()
        self.tool_a = make_tool("cardorder-tool-a", "CardOrder Tool A", published_at=PAST)
        self.tool_b = make_tool("cardorder-tool-b", "CardOrder Tool B", published_at=PAST)
        self.tool_c = make_tool("cardorder-tool-c", "CardOrder Tool C", published_at=PAST)
        self.comparison = make_comparison(
            slug="cardorder-cmp", title="CardOrder Scenario", author=self.author
        )
        self.entry_a = add_entry(self.comparison, self.tool_a, position=10, summary="S")
        self.entry_b = add_entry(self.comparison, self.tool_b, position=20, summary="S")
        self.entry_c = add_entry(self.comparison, self.tool_c, position=30, summary="S")
        self.published = publish(self.comparison, self.author)

    def _order(self, html):
        positions = {
            name: html.find(name)
            for name in ("CardOrder Tool A", "CardOrder Tool B", "CardOrder Tool C")
        }
        return [n for n, _p in sorted(positions.items(), key=lambda kv: kv[1])]

    def test_card_order_unchanged_before_republish(self):
        before = self._order(self.client.get("/en/compare/").content.decode())

        reviewed = start_review_round(self.published, self.author)
        reviewed.tool_entries.filter(pk=self.entry_a.pk).update(position=99)
        reviewed.tool_entries.filter(pk=self.entry_c.pk).update(position=5)

        after = self._order(self.client.get("/en/compare/").content.decode())
        self.assertEqual(before, after)
        self.assertEqual(before, ["CardOrder Tool A", "CardOrder Tool B", "CardOrder Tool C"])

    def test_republish_activates_new_order(self):
        reviewed = start_review_round(self.published, self.author)
        reviewed.tool_entries.filter(pk=self.entry_a.pk).update(position=99)
        reviewed.tool_entries.filter(pk=self.entry_c.pk).update(position=5)
        publish(Comparison.objects.get(pk=self.comparison.pk), self.author)

        after = self._order(self.client.get("/en/compare/").content.decode())
        self.assertEqual(after, ["CardOrder Tool C", "CardOrder Tool B", "CardOrder Tool A"])


class ToolVisibilityCardTests(CardToolTestCase):
    """Group F."""

    def setUp(self):
        super().setUp()
        self.tool_a = make_tool("cardvis-tool-a", "CardVis Tool A", published_at=PAST)
        self.tool_b = make_tool("cardvis-tool-b", "CardVis Tool B", published_at=FUTURE)
        self.tool_c = make_tool("cardvis-tool-c", "CardVis Tool C", published_at=PAST)
        self.comparison = make_comparison(
            slug="cardvis-cmp", title="CardVis Scenario", author=self.author
        )
        add_entry(self.comparison, self.tool_a, position=10, summary="S")
        add_entry(self.comparison, self.tool_b, position=20, summary="S")
        add_entry(self.comparison, self.tool_c, position=30, summary="S")
        self.published = publish(self.comparison, self.author)

    def _html(self):
        return self.client.get("/en/compare/").content.decode()

    def test_hidden_tool_is_skipped_order_preserved(self):
        html = self._html()
        self.assertIn("CardVis Tool A", html)
        self.assertNotIn("CardVis Tool B", html)
        self.assertIn("CardVis Tool C", html)
        self.assertLess(html.find("CardVis Tool A"), html.find("CardVis Tool C"))

    def test_tool_becomes_public_without_republish(self):
        Tool.objects.filter(pk=self.tool_b.pk).update(published_at=PAST)
        html = self._html()
        self.assertIn("CardVis Tool A", html)
        self.assertIn("CardVis Tool B", html)
        self.assertIn("CardVis Tool C", html)

    def test_tool_withdrawn_again(self):
        Tool.objects.filter(pk=self.tool_b.pk).update(published_at=PAST)
        Tool.objects.filter(pk=self.tool_b.pk).update(published_at=FUTURE)
        html = self._html()
        self.assertNotIn("CardVis Tool B", html)
        self.assertIn("CardVis Tool A", html)
        self.assertIn("CardVis Tool C", html)

    def test_deleted_tool_no_error(self):
        Tool.objects.filter(pk=self.tool_b.pk).update(published_at=PAST)
        self.tool_b.delete()
        resp = self.client.get("/en/compare/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("CardVis Tool B", resp.content.decode())


class SnapshotMembershipCardTests(CardToolTestCase):
    """Group G."""

    def setUp(self):
        super().setUp()
        self.tool_a = make_tool("cardmemb-tool-a", "CardMemb Tool A", published_at=PAST)
        self.tool_b = make_tool("cardmemb-tool-b", "CardMemb Tool B", published_at=PAST)
        self.comparison = make_comparison(
            slug="cardmemb-cmp", title="CardMemb Scenario", author=self.author
        )
        add_entry(self.comparison, self.tool_a, position=10, summary="S")
        self.published = publish(self.comparison, self.author)

        reviewed = start_review_round(self.published, self.author)
        add_entry(reviewed, self.tool_b, position=20, summary="S")

    def test_draft_only_tool_has_no_badge(self):
        html = self.client.get("/en/compare/").content.decode()
        self.assertNotIn("CardMemb Tool B", html)

    def test_snapshot_member_tool_gets_a_badge(self):
        cmp2 = make_comparison(slug="cardmemb-cmp2", title="CardMemb Scenario 2", author=self.author)
        tool_c = make_tool("cardmemb-tool-c", "CardMemb Tool C", published_at=FUTURE)
        add_entry(cmp2, tool_c, position=10, summary="S")
        publish(cmp2, self.author)

        self.assertNotIn("CardMemb Tool C", self.client.get("/en/compare/").content.decode())
        Tool.objects.filter(pk=tool_c.pk).update(published_at=PAST)
        self.assertIn("CardMemb Tool C", self.client.get("/en/compare/").content.decode())


class StateCCardTests(CardToolTestCase):
    """Group H."""

    def setUp(self):
        super().setUp()
        self.tool_a = make_tool("cardlegacy-tool-a", "CardLegacy Tool A", published_at=PAST)
        self.comparison = Comparison.objects.create(status="published", published_at=timezone.now())
        self.comparison.create_translation(
            "en", title="CardLegacy Scenario", intro="i", body="b", slug="cardlegacy-cmp"
        )
        self.comparison.tool_entries.create(tool=self.tool_a, position=10).create_translation(
            "en", label="", summary="Legacy summary", pros="", cons="", special=""
        )

    def test_legacy_published_record_shows_badge(self):
        self.assertIsNone(self.comparison.live_entries)
        html = self.client.get("/en/compare/").content.decode()
        self.assertIn("CardLegacy Tool A", html)

    def test_legacy_record_in_review_is_invisible(self):
        fresh = Comparison.objects.get(pk=self.comparison.pk)
        fresh.move_to_review(by=self.author)
        fresh.save()
        html = self.client.get("/en/compare/").content.decode()
        self.assertNotIn("CardLegacy Tool A", html)
        self.assertNotIn("CardLegacy Scenario", html)

    def test_empty_snapshot_list_produces_no_badges(self):
        Comparison.objects.filter(pk=self.comparison.pk).update(live_entries=[])
        html = self.client.get("/en/compare/").content.decode()
        self.assertIn("CardLegacy Scenario", html)
        self.assertNotIn("CardLegacy Tool A", html)


class LanguageCardTests(CardToolTestCase):
    """Group I."""

    def setUp(self):
        super().setUp()
        self.tool = make_tool("cardlang-tool", "CardLang Tool", published_at=PAST)
        self.comparison = make_comparison(
            slug="cardlang-cmp-en", title="CardLang Scenario EN", author=self.author
        )
        add_entry(self.comparison, self.tool, position=10, summary="S")
        self.published = publish(self.comparison, self.author)

    def test_en_card_shows_tool_de_list_does_not(self):
        html_en = self.client.get("/en/compare/").content.decode()
        self.assertIn("CardLang Tool", html_en)
        html_de = self.client.get("/de/compare/").content.decode()
        self.assertNotIn("CardLang Tool", html_de)
        self.assertNotIn("CardLang Scenario", html_de)

    def test_de_translation_only_draft_does_not_activate_de_card(self):
        reviewed = start_review_round(self.published, self.author)
        reviewed.create_translation(
            "de", title="CardLang Scenario DE", intro="i", body="b", slug="cardlang-cmp-de"
        )
        html_de = self.client.get("/de/compare/").content.decode()
        self.assertNotIn("CardLang Tool", html_de)


class SurfaceConsistencyTests(CardToolTestCase):
    """Group J."""

    def setUp(self):
        super().setUp()
        self.tool_a = make_tool("cardconsist-tool-a", "CardConsist Tool A", published_at=PAST)
        self.tool_b = make_tool("cardconsist-tool-b", "CardConsist Tool B", published_at=PAST)
        self.comparison = make_comparison(
            slug="cardconsist-cmp", title="CardConsist Scenario", author=self.author
        )
        self.entry = add_entry(self.comparison, self.tool_a, position=10, summary="S")
        self.published = publish(self.comparison, self.author)

    def test_card_detail_jsonld_all_agree_after_draft_swap(self):
        reviewed = start_review_round(self.published, self.author)
        fresh_entry = reviewed.tool_entries.get(pk=self.entry.pk)
        fresh_entry.tool = self.tool_b
        fresh_entry.save(update_fields=["tool"])

        list_html = self.client.get("/en/compare/").content.decode()
        detail_resp = self.client.get("/en/compare/cardconsist-cmp/")
        detail_html = detail_resp.content.decode()
        json_ld = detail_resp.context["seo"].json_ld
        names = [item["name"] for item in json_ld.get("about", [])]

        for surface, html_or_names in (("card", list_html), ("detail", detail_html)):
            with self.subTest(surface=surface):
                self.assertIn("CardConsist Tool A", html_or_names)
                self.assertNotIn("CardConsist Tool B", html_or_names)
        self.assertIn("CardConsist Tool A", names)
        self.assertNotIn("CardConsist Tool B", names)


class PaginationCardTests(CardToolTestCase):
    """Group K."""

    def test_only_current_page_needs_the_projection_both_pages_correct(self):
        # Zero-padded indices: "CardPage Tool 01" is never a substring of
        # "CardPage Tool 10" the way "CardPage Tool 1" would be.
        tools = []
        comparisons = []
        for i in range(20):
            tool = make_tool(f"cardpage-tool-{i:02d}", f"CardPage Tool {i:02d}", published_at=PAST)
            c = make_comparison(slug=f"cardpage-cmp-{i:02d}", title=f"CardPage {i:02d}", author=self.author)
            add_entry(c, tool, position=10, summary=f"S{i:02d}")
            publish(c, self.author)
            tools.append(tool)
            comparisons.append(c)

        page1 = self.client.get("/en/compare/").content.decode()
        page2 = self.client.get("/en/compare/?page=2").content.decode()

        # Every comparison's own tool badge appears exactly on one of the
        # two pages, never on both, never on neither.
        for i in range(20):
            name = f"CardPage Tool {i:02d}"
            on_p1 = name in page1
            on_p2 = name in page2
            self.assertNotEqual(on_p1, on_p2, f"{name} should appear on exactly one page")


class QueryCountTests(CardToolTestCase):
    """Group L."""

    def _make_published(self, n, prefix):
        for i in range(n):
            tool = make_tool(f"{prefix}-tool-{i}", f"{prefix} Tool {i}", published_at=PAST)
            c = make_comparison(slug=f"{prefix}-cmp-{i}", title=f"{prefix} {i}", author=self.author)
            add_entry(c, tool, position=10, summary=f"S{i}")
            publish(c, self.author)

    def test_projection_query_count_is_constant(self):
        self._make_published(3, "qc-small")
        comparisons_small = list(Comparison.objects.visible_in_language("en"))
        with CaptureQueriesContext(connection) as small_ctx:
            public_tools_for_comparisons(comparisons_small)

        self._make_published(12, "qc-large")
        comparisons_large = list(Comparison.objects.visible_in_language("en"))
        with CaptureQueriesContext(connection) as large_ctx:
            public_tools_for_comparisons(comparisons_large)

        self.assertLessEqual(len(small_ctx.captured_queries), 3)
        self.assertLessEqual(len(large_ctx.captured_queries), 3)

    def test_no_query_when_input_is_empty(self):
        with CaptureQueriesContext(connection) as ctx:
            result = public_tools_for_comparisons([])
        self.assertEqual(result, {})
        self.assertEqual(len(ctx.captured_queries), 0)


class DataIntegrityTests(CardToolTestCase):
    """Group M."""

    def test_repeated_list_gets_do_not_mutate_anything(self):
        tool = make_tool("cardintegrity-tool", "CardIntegrity Tool", published_at=PAST)
        comparison = make_comparison(slug="cardintegrity-cmp", title="CardIntegrity Scenario", author=self.author)
        add_entry(comparison, tool, position=10, summary="S")
        publish(comparison, self.author)

        before = Comparison.objects.get(pk=comparison.pk)
        before_snapshot = before.live_entries
        before_i18n = before.live_i18n
        before_status = before.status
        before_updated = before.updated_at
        before_draft = list(before.tool_entries.values_list("pk", "position", "tool_id"))

        for _ in range(3):
            self.client.get("/en/compare/")

        after = Comparison.objects.get(pk=comparison.pk)
        self.assertEqual(after.live_entries, before_snapshot)
        self.assertEqual(after.live_i18n, before_i18n)
        self.assertEqual(after.status, before_status)
        self.assertEqual(after.updated_at, before_updated)
        self.assertEqual(list(after.tool_entries.values_list("pk", "position", "tool_id")), before_draft)
