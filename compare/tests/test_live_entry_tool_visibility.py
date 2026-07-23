"""
Beta 11.9A: the comparison entry snapshot stores a stable ``tool_id``, but
the tool it points at is resolved from the *current*, always-live Tool
contract (``catalog/models.py::ToolQuerySet.public()``) at render time - see
the presentation module's own docstring for why that resolution is
deliberately not frozen alongside the entry text.

Before this slice ``_entries_from_snapshot()`` resolved those IDs through
the unfiltered ``Tool.objects.in_bulk()``, bypassing that contract entirely.
A comparison entry could therefore render and link a tool that was never
public, not yet public, or withdrawn - independent of the entry snapshot
itself being perfectly correct. This module pins the fix: entry tool
resolution follows ``Tool.objects.public()`` exactly.
"""
from datetime import timedelta

from django.conf import settings
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone, translation

from django.urls import reverse

from catalog.models import Tool
from compare.models import Comparison
from compare.presentation import public_tool_entries
from compare.tests.live_snapshot_fixtures import (
    add_entry,
    make_comparison,
    make_tool,
    make_user,
    publish,
)

PAST = timezone.now() - timedelta(days=1)
FUTURE = timezone.now() + timedelta(days=30)


class EntryToolVisibilityTestCase(TestCase):
    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.author = make_user("cmp-entrytool-author")

    def _html(self, slug):
        return self.client.get(f"/en/compare/{slug}/").content.decode()


class PublicToolEntryTests(EntryToolVisibilityTestCase):
    """Group A: an entry whose tool is public renders exactly as before."""

    def setUp(self):
        super().setUp()
        self.tool = make_tool("evt-public", "EVT Public Tool", published_at=PAST)
        self.comparison = make_comparison(
            slug="evt-public", title="EVT Public", author=self.author
        )
        add_entry(self.comparison, self.tool, position=10, summary="Public summary")
        publish(self.comparison, self.author)

    def test_entry_appears_with_correct_name_and_link(self):
        html = self._html("evt-public")
        self.assertIn("EVT Public Tool", html)
        url = reverse("catalog:detail", kwargs={"slug": self.tool.slug})
        self.assertIn(f'href="{url}"', html)

    def test_projection_returns_the_entry(self):
        comparison = Comparison.objects.get(pk=self.comparison.pk)
        entries = public_tool_entries(comparison, "en")
        self.assertEqual([e.tool.pk for e in entries], [self.tool.pk])

    def test_snapshot_itself_is_unchanged(self):
        before = Comparison.objects.get(pk=self.comparison.pk).live_entries
        public_tool_entries(Comparison.objects.get(pk=self.comparison.pk), "en")
        after = Comparison.objects.get(pk=self.comparison.pk).live_entries
        self.assertEqual(before, after)


class FutureToolTests(EntryToolVisibilityTestCase):
    """Group B: a scheduled (not-yet-public) tool must not leak."""

    def setUp(self):
        super().setUp()
        self.tool = make_tool("evt-future-tool", "EVT Future Tool", published_at=FUTURE)
        self.comparison = make_comparison(
            slug="evt-future-cmp", title="EVT Future Comparison", author=self.author
        )
        add_entry(self.comparison, self.tool, position=10, summary="Future summary")
        publish(self.comparison, self.author)

    def test_entry_does_not_appear(self):
        html = self._html("evt-future-cmp")
        self.assertNotIn("EVT Future Tool", html)
        self.assertNotIn("Future summary", html)
        self.assertNotIn(self.tool.slug, html)

    def test_page_still_renders_without_error(self):
        self.assertEqual(self.client.get("/en/compare/evt-future-cmp/").status_code, 200)

    def test_projection_omits_the_entry(self):
        comparison = Comparison.objects.get(pk=self.comparison.pk)
        self.assertEqual(public_tool_entries(comparison, "en"), [])


class ToolBecomesPublicTests(EntryToolVisibilityTestCase):
    """Group C: the same snapshot re-admits an entry once its tool goes
    public, with no comparison republish."""

    def setUp(self):
        super().setUp()
        self.tool = make_tool("evt-becomes-public-tool", "EVT Becomes Public Tool", published_at=FUTURE)
        self.comparison = make_comparison(
            slug="evt-becomes-public-cmp", title="EVT Becomes Public Comparison", author=self.author
        )
        add_entry(self.comparison, self.tool, position=10, summary="Becomes public summary")
        publish(self.comparison, self.author)

    def test_entry_appears_once_the_tool_is_public(self):
        snapshot_before = Comparison.objects.get(pk=self.comparison.pk).live_entries
        self.assertNotIn("EVT Becomes Public Tool", self._html("evt-becomes-public-cmp"))

        Tool.objects.filter(pk=self.tool.pk).update(published_at=PAST)

        html = self._html("evt-becomes-public-cmp")
        self.assertIn("EVT Becomes Public Tool", html)
        self.assertIn("Becomes public summary", html)
        snapshot_after = Comparison.objects.get(pk=self.comparison.pk).live_entries
        self.assertEqual(snapshot_before, snapshot_after)


class ToolBecomesNonPublicTests(EntryToolVisibilityTestCase):
    """Group D: an already-public tool withdrawn later (published_at moved
    into the future) drops its entry, with no comparison republish."""

    def setUp(self):
        super().setUp()
        self.tool = make_tool("evt-withdrawn-tool", "EVT Withdrawn Tool", published_at=PAST)
        self.comparison = make_comparison(
            slug="evt-withdrawn-cmp", title="EVT Withdrawn Comparison", author=self.author
        )
        add_entry(self.comparison, self.tool, position=10, summary="Withdrawn summary")
        publish(self.comparison, self.author)

    def test_entry_disappears_once_the_tool_is_withdrawn(self):
        self.assertIn("EVT Withdrawn Tool", self._html("evt-withdrawn-cmp"))

        Tool.objects.filter(pk=self.tool.pk).update(published_at=FUTURE)

        html = self._html("evt-withdrawn-cmp")
        self.assertNotIn("EVT Withdrawn Tool", html)
        self.assertNotIn("Withdrawn summary", html)


class MixedSnapshotOrderTests(EntryToolVisibilityTestCase):
    """Group F: a not-public tool in the middle of the snapshot is skipped
    without disturbing the order of the remaining entries."""

    def setUp(self):
        super().setUp()
        self.tool_a = make_tool("evt-mix-a", "EVT Mix A", published_at=PAST)
        self.tool_b = make_tool("evt-mix-b", "EVT Mix B", published_at=FUTURE)
        self.tool_c = make_tool("evt-mix-c", "EVT Mix C", published_at=PAST)
        self.comparison = make_comparison(
            slug="evt-mix", title="EVT Mix", author=self.author
        )
        add_entry(self.comparison, self.tool_a, position=10, summary="Summary A")
        add_entry(self.comparison, self.tool_b, position=20, summary="Summary B")
        add_entry(self.comparison, self.tool_c, position=30, summary="Summary C")
        publish(self.comparison, self.author)

    def test_only_public_tools_remain_in_snapshot_order(self):
        comparison = Comparison.objects.get(pk=self.comparison.pk)
        entries = public_tool_entries(comparison, "en")
        self.assertEqual([e.tool.pk for e in entries], [self.tool_a.pk, self.tool_c.pk])

    def test_rendered_order_matches(self):
        html = self._html("evt-mix")
        pos_a = html.find("EVT Mix A")
        pos_b = html.find("EVT Mix B")
        pos_c = html.find("EVT Mix C")
        self.assertEqual(pos_b, -1)
        self.assertNotEqual(pos_a, -1)
        self.assertNotEqual(pos_c, -1)
        self.assertLess(pos_a, pos_c)


class AllToolsNonPublicTests(EntryToolVisibilityTestCase):
    """Group G: every tool withdrawn leaves an empty, error-free entry
    list - never a fallback to the draft rows."""

    def setUp(self):
        super().setUp()
        self.tool_a = make_tool("evt-none-a", "EVT None A", published_at=FUTURE)
        self.tool_b = make_tool("evt-none-b", "EVT None B", published_at=FUTURE)
        self.comparison = make_comparison(
            slug="evt-none", title="EVT None", author=self.author
        )
        add_entry(self.comparison, self.tool_a, position=10, summary="Summary A")
        add_entry(self.comparison, self.tool_b, position=20, summary="Summary B")
        publish(self.comparison, self.author)

    def test_page_renders_with_no_entries(self):
        resp = self.client.get("/en/compare/evt-none/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertNotIn("EVT None A", html)
        self.assertNotIn("EVT None B", html)

    def test_projection_is_an_empty_list_not_a_draft_fallback(self):
        comparison = Comparison.objects.get(pk=self.comparison.pk)
        self.assertEqual(public_tool_entries(comparison, "en"), [])


class LanguageNeutralToolFilterTests(EntryToolVisibilityTestCase):
    """Group H: tool visibility has no language dimension; entry text still
    comes only from the snapshot, in the requested language."""

    def setUp(self):
        super().setUp()
        self.tool = make_tool("evt-lang", "EVT Lang Tool", published_at=PAST)
        self.comparison = make_comparison(
            slug="evt-lang-en", title="EVT Lang EN", author=self.author
        )
        from compare.tests.live_snapshot_fixtures import add_translation
        add_translation(
            self.comparison, "de", slug="evt-lang-de", title="EVT Lang DE"
        )
        entry = add_entry(
            self.comparison, self.tool, position=10, language="en", summary="EN summary"
        )
        from compare.tests.live_snapshot_fixtures import add_entry_translation
        add_entry_translation(entry, "de", summary="DE summary")
        publish(self.comparison, self.author)

    def test_tool_is_visible_in_both_languages_with_language_specific_text(self):
        comparison = Comparison.objects.get(pk=self.comparison.pk)
        en_entries = public_tool_entries(comparison, "en")
        de_entries = public_tool_entries(comparison, "de")
        self.assertEqual([e.tool.pk for e in en_entries], [self.tool.pk])
        self.assertEqual([e.tool.pk for e in de_entries], [self.tool.pk])
        self.assertEqual(en_entries[0].summary, "EN summary")
        self.assertEqual(de_entries[0].summary, "DE summary")

    def test_withdrawn_tool_disappears_in_every_language(self):
        Tool.objects.filter(pk=self.tool.pk).update(published_at=FUTURE)
        comparison = Comparison.objects.get(pk=self.comparison.pk)
        self.assertEqual(public_tool_entries(comparison, "en"), [])
        self.assertEqual(public_tool_entries(comparison, "de"), [])


class QueryCountTests(EntryToolVisibilityTestCase):
    """Group I: one tool query for the whole snapshot, regardless of size."""

    def setUp(self):
        super().setUp()
        self.comparison = make_comparison(
            slug="evt-queries", title="EVT Queries", author=self.author
        )
        self.tools = [
            make_tool(f"evt-q-{i}", f"EVT Q {i}", published_at=PAST) for i in range(5)
        ]
        for i, tool in enumerate(self.tools):
            add_entry(self.comparison, tool, position=i * 10, summary=f"Summary {i}")
        publish(self.comparison, self.author)

    def test_exactly_one_tool_query_for_five_entries(self):
        comparison = Comparison.objects.get(pk=self.comparison.pk)
        with CaptureQueriesContext(connection) as captured:
            entries = public_tool_entries(comparison, "en")
        self.assertEqual(len(entries), 5)
        tool_queries = [q for q in captured.captured_queries if '"catalog_tool"' in q["sql"]]
        self.assertEqual(len(tool_queries), 1)


class LinkSecurityTests(EntryToolVisibilityTestCase):
    """Group J: a rendered tool link is always the public catalog URL."""

    def setUp(self):
        super().setUp()
        self.public_tool = make_tool("evt-link-public", "EVT Link Public", published_at=PAST)
        self.hidden_tool = make_tool("evt-link-hidden", "EVT Link Hidden", published_at=FUTURE)
        self.comparison = make_comparison(
            slug="evt-link", title="EVT Link", author=self.author
        )
        add_entry(self.comparison, self.public_tool, position=10, summary="Public link summary")
        add_entry(self.comparison, self.hidden_tool, position=20, summary="Hidden link summary")
        publish(self.comparison, self.author)

    def test_public_tool_link_is_the_public_catalog_url(self):
        html = self._html("evt-link")
        url = reverse("catalog:detail", kwargs={"slug": self.public_tool.slug})
        self.assertIn(url, html)
        self.assertNotIn("/admin/", html)
        self.assertNotIn("/preview/", html)

    def test_hidden_tool_has_no_link_at_all(self):
        html = self._html("evt-link")
        self.assertNotIn(self.hidden_tool.slug, html)


class DataIntegrityTests(EntryToolVisibilityTestCase):
    """Group K: reading the projection mutates nothing."""

    def setUp(self):
        super().setUp()
        self.tool = make_tool("evt-integrity", "EVT Integrity", published_at=PAST)
        self.comparison = make_comparison(
            slug="evt-integrity", title="EVT Integrity", author=self.author
        )
        add_entry(self.comparison, self.tool, position=10, summary="Integrity summary")
        publish(self.comparison, self.author)

    def test_repeated_projection_calls_do_not_mutate_state(self):
        before = Comparison.objects.get(pk=self.comparison.pk)
        before_snapshot = before.live_entries
        before_status = before.status
        before_i18n = before.live_i18n
        before_updated = before.updated_at
        before_draft_rows = list(
            before.tool_entries.values_list("pk", "position", "tool_id")
        )

        for _ in range(3):
            public_tool_entries(Comparison.objects.get(pk=self.comparison.pk), "en")

        after = Comparison.objects.get(pk=self.comparison.pk)
        self.assertEqual(after.live_entries, before_snapshot)
        self.assertEqual(after.status, before_status)
        self.assertEqual(after.live_i18n, before_i18n)
        self.assertEqual(after.updated_at, before_updated)
        self.assertEqual(
            list(after.tool_entries.values_list("pk", "position", "tool_id")),
            before_draft_rows,
        )
