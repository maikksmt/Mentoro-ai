"""
Beta 11.9 groups C/D: tool-entry content and structure.

Before this slice ``ComparisonDetailView`` handed the template
``obj.tool_entries.select_related("tool").all()`` - the live rows - so every
unpublished entry edit was public the moment it was saved. This module pins
the published contract for both halves:

* **C - content**: text, per entry, per language.
* **D - structure**: which tools, in which order, added or removed.
"""
from django.conf import settings
from django.test import TestCase
from django.utils import translation

from compare.models import Comparison
from compare.presentation import public_tool_entries
from compare.tests.live_snapshot_fixtures import (
    add_entry,
    make_comparison,
    make_legacy_published,
    make_tool,
    make_user,
    publish,
    request_rework,
    save_entry_draft_edit,
    start_review_round,
)


class EntryContentTests(TestCase):
    """Group C: published entry text survives every draft edit."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.author = make_user("cmp-entry-author")
        self.tool = make_tool("entry-tool-a", "Entry Tool A")

        self.comparison = make_comparison(
            slug="entry-content", title="Entry Content", author=self.author
        )
        self.entry = add_entry(
            self.comparison,
            self.tool,
            position=10,
            label="Live label A",
            summary="<p>Live summary A</p>",
            pros="<ul><li>Live pro A</li></ul>",
            cons="<ul><li>Live con A</li></ul>",
            special="<p>Live special A</p>",
        )
        publish(self.comparison, self.author)

        save_entry_draft_edit(
            self.entry,
            "en",
            label="Draft label B",
            summary="<p>Draft summary B</p>",
            pros="<ul><li>Draft pro B</li></ul>",
            cons="<ul><li>Draft con B</li></ul>",
            special="<p>Draft special B</p>",
        )
        start_review_round(self.comparison, self.author)

    def _html(self):
        return self.client.get("/en/compare/entry-content/").content.decode()

    def test_detail_page_is_still_reachable(self):
        self.assertEqual(self.client.get("/en/compare/entry-content/").status_code, 200)

    def test_every_published_entry_field_is_rendered(self):
        html = self._html()
        for value in (
            "Live label A", "Live summary A", "Live pro A", "Live con A", "Live special A",
        ):
            with self.subTest(value=value):
                self.assertIn(value, html)

    def test_no_draft_entry_field_is_rendered(self):
        html = self._html()
        for value in (
            "Draft label B", "Draft summary B", "Draft pro B", "Draft con B", "Draft special B",
        ):
            with self.subTest(value=value):
                self.assertNotIn(value, html)

    def test_projection_returns_published_values(self):
        comparison = Comparison.objects.get(pk=self.comparison.pk)
        entries = public_tool_entries(comparison, "en")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].label, "Live label A")
        self.assertEqual(entries[0].summary, "<p>Live summary A</p>")
        self.assertEqual(entries[0].tool.pk, self.tool.pk)

    def test_republish_activates_the_new_entry_content(self):
        in_review = Comparison.objects.get(pk=self.comparison.pk)
        publish(in_review, self.author)

        html = self._html()
        self.assertIn("Draft label B", html)
        self.assertIn("Draft summary B", html)
        self.assertNotIn("Live label A", html)


class EntryStructureTests(TestCase):
    """Group D: additions, removals, tool swaps and reordering."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.author = make_user("cmp-struct-author")
        self.editor = make_user("cmp-struct-editor")
        self.tool_a = make_tool("struct-tool-a", "Struct Tool A")
        self.tool_b = make_tool("struct-tool-b", "Struct Tool B")
        self.tool_c = make_tool("struct-tool-c", "Struct Tool C")

        self.comparison = make_comparison(
            slug="entry-structure", title="Entry Structure", author=self.author
        )
        self.entry_a = add_entry(
            self.comparison, self.tool_a, position=10, summary="Summary of A"
        )
        self.entry_b = add_entry(
            self.comparison, self.tool_b, position=20, summary="Summary of B"
        )
        publish(self.comparison, self.author)

    def _html(self):
        return self.client.get("/en/compare/entry-structure/").content.decode()

    def _rendered_tool_order(self, html):
        positions = {
            name: html.find(name)
            for name in ("Struct Tool A", "Struct Tool B", "Struct Tool C")
            if html.find(name) != -1
        }
        return [name for name, _pos in sorted(positions.items(), key=lambda kv: kv[1])]

    def test_published_order_is_rendered(self):
        self.assertEqual(self._rendered_tool_order(self._html()), ["Struct Tool A", "Struct Tool B"])

    def test_draft_reordering_does_not_change_the_public_order(self):
        self.entry_a.position = 99
        self.entry_a.save(update_fields=["position"])
        start_review_round(self.comparison, self.author)

        self.assertEqual(self._rendered_tool_order(self._html()), ["Struct Tool A", "Struct Tool B"])

    def test_a_new_draft_entry_is_not_rendered(self):
        add_entry(self.comparison, self.tool_c, position=30, summary="Summary of C")
        start_review_round(self.comparison, self.author)

        html = self._html()
        self.assertNotIn("Struct Tool C", html)
        self.assertNotIn("Summary of C", html)

    def test_a_removed_draft_entry_stays_public(self):
        self.entry_b.delete()
        start_review_round(self.comparison, self.author)

        html = self._html()
        self.assertIn("Struct Tool B", html)
        self.assertIn("Summary of B", html)

    def test_a_swapped_draft_tool_does_not_change_the_public_tool(self):
        self.entry_b.tool = self.tool_c
        self.entry_b.save(update_fields=["tool"])
        start_review_round(self.comparison, self.author)

        html = self._html()
        self.assertIn("Struct Tool B", html)
        self.assertNotIn("Struct Tool C", html)

    def test_draft_structure_does_not_change_json_ld(self):
        add_entry(self.comparison, self.tool_c, position=30, summary="Summary of C")
        self.entry_a.position = 99
        self.entry_a.save(update_fields=["position"])
        start_review_round(self.comparison, self.author)

        json_ld = self.client.get("/en/compare/entry-structure/").context["seo"].json_ld
        names = [item["name"] for item in json_ld["about"]]
        self.assertEqual(names, ["Struct Tool A", "Struct Tool B"])

    def test_draft_structure_does_not_change_the_header_tool_badges(self):
        add_entry(self.comparison, self.tool_c, position=30, summary="Summary of C")
        start_review_round(self.comparison, self.author)

        tools_list = self.client.get("/en/compare/entry-structure/").context["tools_list"]
        self.assertEqual([t.pk for t in tools_list], [self.tool_a.pk, self.tool_b.pk])

    def test_rework_keeps_the_published_structure(self):
        add_entry(self.comparison, self.tool_c, position=30, summary="Summary of C")
        start_review_round(self.comparison, self.author)
        request_rework(self.comparison, self.editor)

        html = self._html()
        self.assertIn("Struct Tool A", html)
        self.assertIn("Struct Tool B", html)
        self.assertNotIn("Struct Tool C", html)

    def test_republish_activates_addition_removal_and_reordering(self):
        add_entry(self.comparison, self.tool_c, position=5, summary="Summary of C")
        self.entry_b.delete()
        start_review_round(self.comparison, self.author)
        publish(Comparison.objects.get(pk=self.comparison.pk), self.author)

        html = self._html()
        self.assertIn("Struct Tool C", html)
        self.assertIn("Summary of C", html)
        self.assertNotIn("Struct Tool B", html)
        # position=5 sorts C before A.
        self.assertEqual(self._rendered_tool_order(html), ["Struct Tool C", "Struct Tool A"])


class LegacyEntryRenderingTests(TestCase):
    """
    State C: a record published before Beta 11.9 has no entry snapshot and
    keeps rendering its live rows - byte-identically to the pre-11.9 page.
    """

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.author = make_user("cmp-legacyentry-author")
        self.tool = make_tool("legacy-entry-tool", "Legacy Entry Tool")

        self.comparison = make_comparison(
            slug="legacy-entries", title="Legacy Entries", author=self.author
        )
        add_entry(self.comparison, self.tool, position=10, summary="Legacy summary")
        self.legacy = make_legacy_published(self.comparison, self.author)

    def test_legacy_record_renders_its_live_rows(self):
        self.assertIsNone(self.legacy.live_entries)
        html = self.client.get("/en/compare/legacy-entries/").content.decode()
        self.assertIn("Legacy Entry Tool", html)
        self.assertIn("Legacy summary", html)

    def test_projection_uses_the_live_rows_for_a_legacy_record(self):
        comparison = Comparison.objects.get(pk=self.comparison.pk)
        entries = public_tool_entries(comparison, "en")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].summary, "Legacy summary")

    def test_an_empty_snapshot_is_authoritative_and_not_a_legacy_fallback(self):
        """``[]`` means "published with no entries" - it must not fall back
        to the live rows the way ``None`` does."""
        Comparison.objects.filter(pk=self.comparison.pk).update(live_entries=[])
        comparison = Comparison.objects.get(pk=self.comparison.pk)

        self.assertEqual(public_tool_entries(comparison, "en"), [])
        html = self.client.get("/en/compare/legacy-entries/").content.decode()
        self.assertNotIn("Legacy summary", html)


class DeletedToolTests(TestCase):
    """Group I: a tool removed from the catalogue after publication."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.author = make_user("cmp-deltool-author")
        self.tool_a = make_tool("deltool-a", "DelTool A")
        self.tool_b = make_tool("deltool-b", "DelTool B")

        self.comparison = make_comparison(
            slug="deleted-tool", title="Deleted Tool", author=self.author
        )
        add_entry(self.comparison, self.tool_a, position=10, summary="Summary A")
        add_entry(self.comparison, self.tool_b, position=20, summary="Summary B")
        publish(self.comparison, self.author)

    def test_a_deleted_tool_is_skipped_without_breaking_the_page(self):
        self.tool_b.delete()

        resp = self.client.get("/en/compare/deleted-tool/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("DelTool A", html)
        self.assertNotIn("Summary B", html)

    def test_projection_skips_the_dangling_snapshot_entry(self):
        self.tool_b.delete()
        comparison = Comparison.objects.get(pk=self.comparison.pk)
        entries = public_tool_entries(comparison, "en")
        self.assertEqual([e.tool.pk for e in entries], [self.tool_a.pk])
