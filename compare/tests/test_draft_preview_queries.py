"""
Beta 11.10A/11.10B: automated query-regression protection for
``compare.presentation.draft_tool_entries()`` and the full comparison
draft-preview render path.

Empirical baseline, Beta 11.10A (before any production code changed):

* ``draft_tool_entries(comparison, language_code)`` issued exactly two
  *content* queries, regardless of entry count - one for the current
  ``ComparisonToolEntry`` rows joined with their ``Tool`` via
  ``select_related("tool")``, one for every entry's translation rows via
  ``prefetch_related("translations")``.
* Subsequently accessing a resolved tool's translated fields (``tool.name``,
  transitively ``tool.get_absolute_url()``) triggered django-parler's own
  per-instance translation cache, which - because this project configures
  ``CACHES["default"]`` as a ``DatabaseCache`` - showed up as one additional
  SQL statement per *distinct tool instance* against the cache table. Beta
  11.10A classified this as acceptable, sitewide, pre-existing behaviour
  (the public detail page has the exact same characteristic).

Beta 11.10B revisited that classification for the *draft preview specifically*
and found it to be a real, distinct-tool-count-scaling N+1 against the
cache table - unacceptable for a preview page that must not slow down as an
editor adds tools. The fix: ``draft_tool_entries()`` now bulk-resolves every
distinct tool's display name in exactly one extra query against
``catalog_tool_translation`` (via ``catalog.models.ToolTranslation``,
reproducing ``Tool.name``'s own language-then-configured-fallback chain) and
returns each entry's tool as an already-materialized, cache-free
``compare.presentation.DraftToolView`` instead of the lazy ``Tool`` model
instance - see that class's and ``_resolve_draft_tools()``'s docstrings.

Current, Beta 11.10B baseline:

* ``draft_tool_entries()`` issues exactly *three* content queries,
  regardless of entry count *or* distinct-tool count - the same two as
  before, plus one bulk tool-translation query.
* Accessing ``tool.name``, ``tool.slug`` or ``tool.get_absolute_url()`` on
  any returned entry costs **zero** further queries of any kind, including
  against the cache table - not "acceptable cache scaling" as under Beta
  11.10A, but genuinely zero, because there is nothing left to resolve
  lazily.
"""
from datetime import timedelta

from django.conf import settings
from django.contrib.sessions.middleware import SessionMiddleware
from django.db import connection
from django.shortcuts import render
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone, translation
from reversion.models import Version

from catalog.models import Tool
from compare.admin import build_comparison_draft_preview_context
from compare.models import Comparison, ComparisonToolEntry
from compare.presentation import draft_tool_entries
from core.services import get_public_inventory
from compare.tests.draft_preview_fixtures import (
    add_entry,
    make_draft_comparison,
    make_tool,
    make_user,
    publish,
)

PAST = timezone.now() - timedelta(days=1)
FUTURE = timezone.now() + timedelta(days=30)

ENTRY_TABLE = ComparisonToolEntry._meta.db_table
ENTRY_TRANSLATION_TABLE = ComparisonToolEntry._parler_meta.root_model._meta.db_table
TOOL_TABLE = Tool._meta.db_table
TOOL_TRANSLATION_TABLE = Tool._parler_meta.root_model._meta.db_table
CACHE_TABLE = settings.CACHES["default"].get("LOCATION", "")


def _classify_queries(captured_queries):
    """
    Buckets captured SQL statements by the real table they touch, using
    live model/cache metadata (``_meta.db_table``,
    ``_parler_meta.root_model``, ``settings.CACHES``) rather than a
    hardcoded table name - see module docstring for why the cache bucket is
    tracked separately instead of asserted to be zero.

    Order matters: ``catalog_tool`` is a substring of
    ``catalog_tool_translation`` (same for the entry/entry-translation
    pair), so the translation-table check must run first.
    """
    buckets = {
        "entry": [],
        "entry_translation": [],
        "tool_translation": [],
        "tool": [],
        "cache": [],
        "other": [],
    }
    for q in captured_queries:
        sql = q["sql"]
        if ENTRY_TRANSLATION_TABLE in sql:
            buckets["entry_translation"].append(sql)
        elif ENTRY_TABLE in sql:
            buckets["entry"].append(sql)
        elif TOOL_TRANSLATION_TABLE in sql:
            buckets["tool_translation"].append(sql)
        elif CACHE_TABLE and CACHE_TABLE in sql:
            buckets["cache"].append(sql)
        elif TOOL_TABLE in sql:
            buckets["tool"].append(sql)
        else:
            buckets["other"].append(sql)
    return buckets


def _content_query_count(buckets):
    """Entry + entry-translation + tool + tool-translation queries -
    everything except cache-table reads and unrelated ("other") queries."""
    return (
        len(buckets["entry"])
        + len(buckets["entry_translation"])
        + len(buckets["tool"])
        + len(buckets["tool_translation"])
    )


class HelperSingleEntryQueryTests(TestCase):
    """Group (Phase 5): the helper with exactly one entry."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.author = make_user("qsingle-author")
        self.tool = make_tool("qsingle-tool", "QSingle Tool", published_at=PAST)
        self.comparison = make_draft_comparison(
            self.author, slug="qsingle-cmp", title="QSingle Scenario"
        )
        add_entry(self.comparison, self.tool, position=10, summary="QSingle summary")

    def test_helper_issues_exactly_three_content_queries(self):
        with CaptureQueriesContext(connection) as captured:
            entries = list(draft_tool_entries(self.comparison, "en"))
        buckets = _classify_queries(captured.captured_queries)

        self.assertEqual(len(entries), 1)
        self.assertEqual(len(buckets["entry"]), 1, "expected exactly one entry(+tool join) query")
        self.assertEqual(len(buckets["entry_translation"]), 1, "expected exactly one translation prefetch query")
        self.assertEqual(len(buckets["tool"]), 0, "no separate tool query - must come from the join")
        self.assertEqual(len(buckets["tool_translation"]), 1, "expected exactly one bulk tool-name resolution query")
        self.assertEqual(_content_query_count(buckets), 3)
        self.assertEqual(len(buckets["cache"]), 0, "materialized tool must never touch the parler cache table")

    def test_entry_query_joins_the_tool_table_directly(self):
        with CaptureQueriesContext(connection) as captured:
            list(draft_tool_entries(self.comparison, "en"))
        buckets = _classify_queries(captured.captured_queries)
        entry_sql = buckets["entry"][0]
        self.assertIn(ENTRY_TABLE, entry_sql)
        self.assertIn(TOOL_TABLE, entry_sql)

    def test_materialized_scalar_fields_cost_no_further_queries(self):
        """pk/slug/label/summary/pros/cons/special are either plain,
        already-joined model fields or plain str attributes on the frozen
        dataclass - none of them are translated, so none should touch the
        database at all once the helper has returned."""
        entries = list(draft_tool_entries(self.comparison, "en"))
        with CaptureQueriesContext(connection) as captured:
            for e in entries:
                _ = (e.tool.pk, e.tool.slug, e.label, e.summary, e.pros, e.cons, e.special)
        self.assertEqual(len(captured.captured_queries), 0)

    def test_tool_name_and_url_access_costs_no_further_queries_at_all(self):
        """Beta 11.10B: ``tool.name``, ``tool.slug`` and
        ``tool.get_absolute_url()`` are plain attribute reads on the
        already-materialized ``DraftToolView`` dataclass, not a lazy Parler
        field descriptor - so unlike Beta 11.10A's baseline (where this cost
        one DatabaseCache read per distinct tool), accessing them now costs
        precisely zero queries, including against the cache table."""
        entries = list(draft_tool_entries(self.comparison, "en"))
        with CaptureQueriesContext(connection) as captured:
            for e in entries:
                _ = e.tool.name
                _ = e.tool.slug
                _ = e.tool.get_absolute_url()
        self.assertEqual(len(captured.captured_queries), 0)

    def test_only_the_requested_language_is_touched(self):
        with CaptureQueriesContext(connection) as captured:
            list(draft_tool_entries(self.comparison, "en"))
        for q in captured.captured_queries:
            self.assertNotIn("'de'", q["sql"])


class HelperMixedEntriesQueryTests(TestCase):
    """Group (Phase 6): twelve entries with mixed EN/DE translations, a
    future tool, and an entry with no translation at all."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.author = make_user("qmixed-author")
        self.comparison = make_draft_comparison(
            self.author, slug="qmixed-cmp", title="QMixed Scenario"
        )

        self.en_only_tools = []
        for i in range(5):
            tool = make_tool(f"qmixed-enonly-{i}", f"QMixed EnOnly {i}", published_at=PAST)
            self.en_only_tools.append(tool)
            add_entry(self.comparison, tool, position=10 + i, language="en", summary=f"EN only {i}")

        self.de_only_tools = []
        for i in range(3):
            tool = make_tool(f"qmixed-deonly-{i}", f"QMixed DeOnly {i}", published_at=PAST)
            self.de_only_tools.append(tool)
            entry = self.comparison.tool_entries.create(tool=tool, position=100 + i)
            entry.create_translation("de", label="", summary=f"DE only {i}", pros="", cons="", special="")

        self.both_tools = []
        for i in range(2):
            tool = make_tool(f"qmixed-both-{i}", f"QMixed Both {i}", published_at=PAST)
            self.both_tools.append(tool)
            entry = add_entry(
                self.comparison, tool, position=200 + i, language="en", summary=f"Both en {i}"
            )
            entry.create_translation("de", label="", summary=f"Both de {i}", pros="", cons="", special="")

        self.untranslated_tool = make_tool("qmixed-untranslated", "QMixed Untranslated", published_at=PAST)
        self.comparison.tool_entries.create(tool=self.untranslated_tool, position=300)

        self.future_tool = make_tool("qmixed-future", "QMixed Future Tool", published_at=FUTURE)
        add_entry(self.comparison, self.future_tool, position=400, language="en", summary="Future summary")

        # 5 + 3 + 2 + 1 + 1 = 12 entries total.
        self.assertEqual(self.comparison.tool_entries.count(), 12)

    def test_en_call_returns_only_explicitly_en_translated_entries(self):
        entries = draft_tool_entries(self.comparison, "en")
        names = {e.tool.name for e in entries}
        self.assertEqual(
            names,
            {t.name for t in self.en_only_tools}
            | {t.name for t in self.both_tools}
            | {self.future_tool.name},
        )
        for t in self.de_only_tools:
            self.assertNotIn(t.name, names)
        self.assertNotIn(self.untranslated_tool.name, names)

    def test_de_call_returns_only_explicitly_de_translated_entries_no_en_fallback(self):
        entries = draft_tool_entries(self.comparison, "de")
        names = {e.tool.name for e in entries}
        self.assertEqual(
            names,
            {t.name for t in self.de_only_tools} | {t.name for t in self.both_tools},
        )
        for t in self.en_only_tools:
            self.assertNotIn(t.name, names)
        self.assertNotIn(self.future_tool.name, names)

    def test_en_call_content_query_count_matches_single_entry_case(self):
        with CaptureQueriesContext(connection) as captured:
            list(draft_tool_entries(self.comparison, "en"))
        buckets = _classify_queries(captured.captured_queries)
        self.assertEqual(len(buckets["entry"]), 1)
        self.assertEqual(len(buckets["entry_translation"]), 1)
        self.assertEqual(len(buckets["tool_translation"]), 1)
        self.assertEqual(_content_query_count(buckets), 3)
        self.assertEqual(len(buckets["cache"]), 0)

    def test_de_call_content_query_count_matches_single_entry_case(self):
        with CaptureQueriesContext(connection) as captured:
            list(draft_tool_entries(self.comparison, "de"))
        buckets = _classify_queries(captured.captured_queries)
        self.assertEqual(_content_query_count(buckets), 3)
        self.assertEqual(len(buckets["cache"]), 0)

    def test_order_is_position_then_pk(self):
        entries = draft_tool_entries(self.comparison, "en")
        rows = list(
            self.comparison.tool_entries.filter(
                pk__in=[
                    e.pk for e in self.comparison.tool_entries.all()
                    if e.tool.name in {v.tool.name for v in entries}
                ]
            ).order_by("position", "pk")
        )
        self.assertEqual([e.tool.name for e in entries], [r.tool.name for r in rows])


class HelperScalingQueryTests(TestCase):
    """Group (Phase 7): 1 vs 5 vs 12 entries - content query count and
    query class must stay identical."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.author = make_user("qscale-author")

    def _make_comparison_with_entries(self, n, prefix):
        comparison = make_draft_comparison(
            self.author, slug=f"{prefix}-cmp", title=f"{prefix} Scenario"
        )
        for i in range(n):
            tool = make_tool(f"{prefix}-tool-{i}", f"{prefix} Tool {i}", published_at=PAST)
            add_entry(comparison, tool, position=10 + i, summary=f"Summary {i}")
        return comparison

    def _content_counts_for(self, n, prefix):
        comparison = self._make_comparison_with_entries(n, prefix)
        with CaptureQueriesContext(connection) as captured:
            entries = list(draft_tool_entries(comparison, "en"))
        self.assertEqual(len(entries), n)
        buckets = _classify_queries(captured.captured_queries)
        return buckets

    def test_content_query_count_is_constant_across_1_5_and_12_entries(self):
        b1 = self._content_counts_for(1, "qscale1")
        b5 = self._content_counts_for(5, "qscale5")
        b12 = self._content_counts_for(12, "qscale12")

        for label, buckets in (("1", b1), ("5", b5), ("12", b12)):
            with self.subTest(entries=label):
                self.assertEqual(len(buckets["entry"]), 1)
                self.assertEqual(len(buckets["entry_translation"]), 1)
                self.assertEqual(len(buckets["tool"]), 0)
                self.assertEqual(len(buckets["tool_translation"]), 1, "one bulk resolution regardless of distinct-tool count")
                self.assertEqual(_content_query_count(buckets), 3)
                self.assertEqual(len(buckets["cache"]), 0, "no per-tool DatabaseCache reads, at 1, 5 or 12 distinct tools")

    def test_entry_and_translation_queries_use_bundled_in_clauses_not_per_row(self):
        """A per-entry/per-tool query pattern would show up as N separate
        near-identical statements; a bundled query shows up as exactly one -
        already proven by the count assertion above, this additionally
        confirms the *entry* translation query and the *tool* translation
        query are both an IN(...)-style bulk fetch rather than N single-row
        lookups, by inspecting their SQL shape."""
        comparison = self._make_comparison_with_entries(12, "qscaleshape")
        with CaptureQueriesContext(connection) as captured:
            list(draft_tool_entries(comparison, "en"))
        buckets = _classify_queries(captured.captured_queries)
        translation_sql = buckets["entry_translation"][0].upper()
        self.assertIn(" IN (", translation_sql)
        tool_translation_sql = buckets["tool_translation"][0].upper()
        self.assertIn(" IN (", tool_translation_sql)

    def test_shared_tool_across_comparisons_resolved_independently_no_leak(self):
        """``ComparisonToolEntry`` has ``unique_together = ("comparison",
        "tool")`` - a tool can never repeat within one comparison's own
        entries, so the realistic "duplicate tool" scenario is the same
        tool referenced by two different comparisons. Each call must still
        resolve in exactly one bulk tool-translation query, with no
        cross-call state leaking between them."""
        shared_tool = make_tool("qscale-shared-tool", "QScale Shared Tool", published_at=PAST)
        cmp1 = make_draft_comparison(self.author, slug="qscale-shared-cmp-1", title="QScale Shared 1")
        cmp2 = make_draft_comparison(self.author, slug="qscale-shared-cmp-2", title="QScale Shared 2")
        add_entry(cmp1, shared_tool, position=10, summary="Shared summary 1")
        add_entry(cmp2, shared_tool, position=10, summary="Shared summary 2")

        with CaptureQueriesContext(connection) as captured:
            views1 = list(draft_tool_entries(cmp1, "en"))
            views2 = list(draft_tool_entries(cmp2, "en"))

        self.assertEqual(len(views1), 1)
        self.assertEqual(len(views2), 1)
        self.assertEqual(views1[0].tool.name, "QScale Shared Tool")
        self.assertEqual(views2[0].tool.name, "QScale Shared Tool")

        buckets = _classify_queries(captured.captured_queries)
        self.assertEqual(len(buckets["tool_translation"]), 2, "one bulk resolution per call, not shared or skipped")
        self.assertEqual(len(buckets["cache"]), 0)


class TemplateRenderQueryTests(TestCase):
    """Group (Phase 8): rendering the real detail template against an
    already-fully-built preview context must not touch the entry, entry-
    translation or tool content tables at all.

    ``base.html``'s chrome renders ``core.services.get_public_inventory()``
    - a site-wide, 300s-cached "N tools / M guides / ..." block present on
    every page, entirely unrelated to comparison entries (Beta 8.7). On a
    cold cache it recomputes via ``Tool.objects.public()`` and a
    per-category tool count, both of which happen to touch ``catalog_tool``
    - not an entry-count-dependent query, but enough to pollute the "tool"
    bucket if the cache is cold. Warming it once in setUp (a real,
    unmodified call to the same cached function every page already uses)
    keeps the measured render isolated to what this slice actually
    protects, exactly as the task allows for "nachweislich aus einer
    unveränderten globalen Templatekomponente" queries.
    """

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.editor = make_user("qtemplate-editor", group="Editor")
        self.comparison = make_draft_comparison(
            self.editor, slug="qtemplate-cmp", title="QTemplate Scenario"
        )
        for i in range(6):
            tool = make_tool(f"qtemplate-tool-{i}", f"QTemplate Tool {i}", published_at=PAST)
            add_entry(self.comparison, tool, position=10 + i, summary=f"QTemplate summary {i}")

        get_public_inventory("en")

    def _request(self):
        request = RequestFactory().get(
            reverse("admin:compare_comparison_draft_preview", args=[self.comparison.pk, "en"])
        )
        SessionMiddleware(lambda r: None).process_request(request)
        request.session.save()
        request.user = self.editor
        return request

    def test_render_alone_touches_no_entry_or_tool_content_table(self):
        # Context build happens entirely OUTSIDE the capture window, exactly
        # as the task requires - only the render itself is measured.
        context = build_comparison_draft_preview_context(self.comparison, "en")
        request = self._request()

        with CaptureQueriesContext(connection) as captured:
            response = render(request, "compare/comparison_detail.html", context)
        html = response.content.decode()

        buckets = _classify_queries(captured.captured_queries)
        self.assertEqual(len(buckets["entry"]), 0, "template render must not query entries")
        self.assertEqual(len(buckets["entry_translation"]), 0, "template render must not query entry translations")
        self.assertEqual(len(buckets["tool"]), 0, "template render must not query the tool content table")
        self.assertEqual(len(buckets["tool_translation"]), 0, "template render must not query tool translations")

        for i in range(6):
            self.assertIn(f"QTemplate Tool {i}", html)
            self.assertIn(f"QTemplate summary {i}", html)

    def test_render_query_count_does_not_scale_with_entry_count(self):
        """Same assertion as above, phrased as a scaling comparison: a
        1-entry comparison's render and a 6-entry comparison's render must
        touch the same (zero) number of entry/tool content queries."""
        small = make_draft_comparison(self.editor, slug="qtemplate-small", title="QTemplate Small")
        tool = make_tool("qtemplate-small-tool", "QTemplate Small Tool", published_at=PAST)
        add_entry(small, tool, position=10, summary="Small summary")

        for comparison, label in ((small, "small"), (self.comparison, "large")):
            context = build_comparison_draft_preview_context(comparison, "en")
            request = self._request()
            with CaptureQueriesContext(connection) as captured:
                render(request, "compare/comparison_detail.html", context)
            buckets = _classify_queries(captured.captured_queries)
            with self.subTest(comparison=label):
                self.assertEqual(_content_query_count(buckets), 0)
                self.assertEqual(len(buckets["entry"]), 0)
                self.assertEqual(len(buckets["entry_translation"]), 0)


class FullPreviewRequestQueryTests(TestCase):
    """Group (Phase 9): the complete HTTP preview request, Small (1 entry)
    versus Large (12 mixed entries)."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.editor = make_user("qfull-editor", group="Editor")
        self.client.force_login(self.editor)

        self.small = make_draft_comparison(
            self.editor, slug="qfull-small-cmp", title="QFull Small Scenario"
        )
        self.small_tool = make_tool("qfull-small-tool", "QFull Small Tool", published_at=PAST)
        add_entry(self.small, self.small_tool, position=10, summary="QFull small summary")

        self.large = make_draft_comparison(
            self.editor, slug="qfull-large-cmp", title="QFull Large Scenario"
        )
        self.large_en_tools = []
        for i in range(9):
            tool = make_tool(f"qfull-large-tool-{i}", f"QFull Large Tool {i}", published_at=PAST)
            self.large_en_tools.append(tool)
            add_entry(self.large, tool, position=10 + i, summary=f"QFull large summary {i}")
        # A couple of DE-only entries the EN preview must fail-closed omit.
        for i in range(2):
            tool = make_tool(f"qfull-large-detool-{i}", f"QFull Large DeTool {i}", published_at=PAST)
            entry = self.large.tool_entries.create(tool=tool, position=100 + i)
            entry.create_translation("de", label="", summary=f"DE only {i}", pros="", cons="", special="")
        # A future tool - visible in the private preview.
        self.future_tool = make_tool("qfull-large-future", "QFull Large Future Tool", published_at=FUTURE)
        add_entry(self.large, self.future_tool, position=200, summary="QFull future summary")
        self.assertEqual(self.large.tool_entries.count(), 12)

        # Warm-up request against a throwaway comparison: absorbs any
        # one-time template-loader/ContentType/parler process-level cache
        # population so it cannot be mistaken for entry-count-driven cost.
        warmup = make_draft_comparison(self.editor, slug="qfull-warmup-cmp", title="QFull Warmup")
        warmup_tool = make_tool("qfull-warmup-tool", "QFull Warmup Tool", published_at=PAST)
        add_entry(warmup, warmup_tool, position=10, summary="warmup")
        self.client.get(
            reverse("admin:compare_comparison_draft_preview", args=[warmup.pk, "en"])
        )

    def _preview_url(self, comparison, language_code="en"):
        return reverse("admin:compare_comparison_draft_preview", args=[comparison.pk, language_code])

    def test_both_previews_render_successfully_with_correct_language_content(self):
        small_resp = self.client.get(self._preview_url(self.small))
        large_resp = self.client.get(self._preview_url(self.large))

        self.assertEqual(small_resp.status_code, 200)
        self.assertEqual(large_resp.status_code, 200)

        small_html = small_resp.content.decode()
        large_html = large_resp.content.decode()
        self.assertIn("QFull Small Tool", small_html)

        for tool in self.large_en_tools:
            self.assertIn(tool.name, large_html)
        self.assertIn("QFull Large Future Tool", large_html)
        self.assertNotIn("QFull Large DeTool 0", large_html)
        self.assertNotIn("QFull Large DeTool 1", large_html)

    def test_headers_are_identical_in_contract_for_small_and_large(self):
        for comparison, label in ((self.small, "small"), (self.large, "large")):
            resp = self.client.get(self._preview_url(comparison))
            with self.subTest(comparison=label):
                self.assertEqual(resp["Content-Language"], "en")
                self.assertEqual(resp["X-Robots-Tag"], "noindex,nofollow")
                for directive in ("no-store", "private", "no-cache", "must-revalidate", "max-age=0"):
                    self.assertIn(directive, resp["Cache-Control"])

    def test_content_relevant_query_classes_match_between_small_and_large(self):
        with CaptureQueriesContext(connection) as small_captured:
            small_resp = self.client.get(self._preview_url(self.small))
        with CaptureQueriesContext(connection) as large_captured:
            large_resp = self.client.get(self._preview_url(self.large))

        self.assertEqual(small_resp.status_code, 200)
        self.assertEqual(large_resp.status_code, 200)

        small_buckets = _classify_queries(small_captured.captured_queries)
        large_buckets = _classify_queries(large_captured.captured_queries)

        # The core Beta 11.10A guarantee: exactly one entry(+tool-join)
        # query and one entry-translation query, for 1 entry and for 12.
        self.assertEqual(len(small_buckets["entry"]), 1)
        self.assertEqual(len(large_buckets["entry"]), 1)
        self.assertEqual(len(small_buckets["entry_translation"]), 1)
        self.assertEqual(len(large_buckets["entry_translation"]), 1)
        self.assertEqual(len(small_buckets["tool"]), 0)
        self.assertEqual(len(large_buckets["tool"]), 0)
        # Beta 11.10B: exactly one bulk tool-name resolution query, for 1
        # distinct tool and for 11 (9 EN-only + 2 DE-only + 1 future) -
        # never one per tool.
        self.assertEqual(len(small_buckets["tool_translation"]), 1)
        self.assertEqual(len(large_buckets["tool_translation"]), 1)

        # Beta 11.10B: the cache bucket no longer scales with distinct-tool
        # count at all (Beta 11.10A's "acceptable" per-tool DatabaseCache
        # reads are gone) - whatever cache reads remain come only from
        # unrelated, unchanged page chrome (e.g. get_public_inventory()),
        # so small and large must show the same count.
        self.assertEqual(len(large_buckets["cache"]), len(small_buckets["cache"]))

        # Sanity ceiling on the whole request (session, auth, admin
        # permission check, comparison lookup, related content, page
        # chrome) - generous, not a fragile exact total.
        self.assertLessEqual(len(large_captured.captured_queries), len(small_captured.captured_queries) + 10)

    def test_public_pages_stay_on_their_own_live_state(self):
        self.client.get(self._preview_url(self.large))
        # Neither comparison was ever published - both must 404 publicly.
        self.assertEqual(self.client.get("/en/compare/qfull-small-cmp/").status_code, 404)
        self.assertEqual(self.client.get("/en/compare/qfull-large-cmp/").status_code, 404)


class DataIntegrityTests(TestCase):
    """Group (Phase 10): the new query-focused calls change nothing."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.editor = make_user("qintegrity-editor", group="Editor")
        self.client.force_login(self.editor)

        self.comparison = make_draft_comparison(
            self.editor, slug="qintegrity-cmp", title="QIntegrity Scenario"
        )
        self.entries = []
        for i in range(12):
            tool = make_tool(f"qintegrity-tool-{i}", f"QIntegrity Tool {i}", published_at=PAST)
            self.entries.append(
                add_entry(self.comparison, tool, position=10 + i, summary=f"QIntegrity summary {i}")
            )
        self.comparison = publish(self.comparison, self.editor)

    def _snapshot_state(self):
        comparison = Comparison.objects.get(pk=self.comparison.pk)
        return {
            "status": comparison.status,
            "live_i18n": comparison.live_i18n,
            "live_entries": comparison.live_entries,
            "last_published_revision_id": comparison.last_published_revision_id,
            "updated_at": comparison.updated_at,
            "entries": list(
                comparison.tool_entries.order_by("position", "pk").values_list("pk", "position", "tool_id")
            ),
            "entry_translations": [
                list(e.translations.values_list("language_code", "summary").order_by("language_code"))
                for e in comparison.tool_entries.order_by("position", "pk")
            ],
            "revision_count": Version.objects.get_for_object(comparison).count(),
        }

    def test_helper_calls_do_not_mutate_state(self):
        before = self._snapshot_state()
        for _ in range(3):
            list(draft_tool_entries(self.comparison, "en"))
            list(draft_tool_entries(self.comparison, "de"))
        self.assertEqual(before, self._snapshot_state())

    def test_context_builder_calls_do_not_mutate_state(self):
        before = self._snapshot_state()
        for _ in range(2):
            build_comparison_draft_preview_context(self.comparison, "en")
        self.assertEqual(before, self._snapshot_state())

    def test_repeated_full_preview_requests_do_not_mutate_state(self):
        before = self._snapshot_state()
        url = reverse("admin:compare_comparison_draft_preview", args=[self.comparison.pk, "en"])
        for _ in range(3):
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200)
        self.assertEqual(before, self._snapshot_state())

    def test_no_extra_revision_created_by_query_test_calls(self):
        before = Version.objects.count()
        list(draft_tool_entries(self.comparison, "en"))
        build_comparison_draft_preview_context(self.comparison, "en")
        self.client.get(
            reverse("admin:compare_comparison_draft_preview", args=[self.comparison.pk, "en"])
        )
        self.assertEqual(Version.objects.count(), before)
