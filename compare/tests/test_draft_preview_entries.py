"""
Beta 11.10 groups D/F/G: the preview's tool-entry contract - the part of
Comparison with no equivalent in Guide/Prompt/UseCase.

Unlike the public detail page (which reads ``Comparison.live_entries``,
Beta 11.9), the private draft preview must show the currently *saved*
``ComparisonToolEntry`` rows: a draft tool swap, a brand-new draft entry, a
deleted entry, or a reordered entry list must all be visible immediately,
without a republish - the exact inverse of the public contract. See
``compare/presentation.py::draft_tool_entries()``.
"""
from datetime import timedelta

from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone, translation

from catalog.models import Tool
from compare.models import Comparison
from compare.tests.draft_preview_fixtures import (
    add_entry,
    make_draft_comparison,
    make_tool,
    make_user,
    publish,
    start_review_round,
)

PAST = timezone.now() - timedelta(days=1)
FUTURE = timezone.now() + timedelta(days=30)


def preview_url(comparison_pk, language_code="en"):
    return reverse("admin:compare_comparison_draft_preview", args=[comparison_pk, language_code])


class EntryDraftValueTests(TestCase):
    """Group D: entry-level draft edits appear in the preview immediately,
    while the public page stays on the published snapshot."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.editor = make_user("entry-editor", group="Editor")
        self.client.force_login(self.editor)

        self.tool_a = make_tool("entrydraft-tool-a", "EntryDraft Tool A", published_at=PAST)
        self.tool_b = make_tool("entrydraft-tool-b", "EntryDraft Tool B", published_at=PAST)
        self.tool_c = make_tool("entrydraft-tool-c", "EntryDraft Tool C", published_at=PAST)

        self.comparison = make_draft_comparison(
            self.editor, slug="entrydraft-cmp", title="EntryDraft Scenario"
        )
        self.entry_a = add_entry(
            self.comparison, self.tool_a, position=10, summary="Summary A"
        )
        self.entry_removed = add_entry(
            self.comparison, self.tool_b, position=20, summary="Summary Removed"
        )
        self.comparison = publish(self.comparison, self.editor)

    def _draft(self):
        reviewed = start_review_round(self.comparison, self.editor)
        return Comparison.objects.get(pk=reviewed.pk)

    def test_public_page_shows_the_published_entries(self):
        html = self.client.get("/en/compare/entrydraft-cmp/").content.decode()
        self.assertIn("EntryDraft Tool A", html)
        self.assertIn("EntryDraft Tool B", html)

    def test_preview_shows_toolswap_new_entry_deletion_and_reorder(self):
        reviewed = self._draft()

        # Tool swap on entry A: A -> C
        fresh_entry_a = reviewed.tool_entries.get(pk=self.entry_a.pk)
        fresh_entry_a.tool = self.tool_c
        fresh_entry_a.save(update_fields=["tool"])

        # Delete the second entry (tool B)
        reviewed.tool_entries.get(pk=self.entry_removed.pk).delete()

        # New entry
        add_entry(
            reviewed, self.tool_b, position=5, summary="Summary New B"
        )

        preview_html = self.client.get(preview_url(self.comparison.pk)).content.decode()
        self.assertIn("EntryDraft Tool C", preview_html)  # swapped-in tool
        self.assertIn("EntryDraft Tool B", preview_html)  # re-added via new entry
        self.assertIn("Summary New B", preview_html)
        # Draft order: new_entry (position=5) before the swapped entry (10)
        self.assertLess(
            preview_html.find("EntryDraft Tool B"),
            preview_html.find("EntryDraft Tool C"),
        )

        # Public page is untouched by any of this - still the original snapshot.
        public_html = self.client.get("/en/compare/entrydraft-cmp/").content.decode()
        self.assertIn("EntryDraft Tool A", public_html)
        self.assertIn("EntryDraft Tool B", public_html)
        self.assertNotIn("EntryDraft Tool C", public_html)
        self.assertNotIn("Summary New B", public_html)

    def test_preview_text_changes_appear_immediately(self):
        reviewed = self._draft()
        fresh_entry_a = reviewed.tool_entries.get(pk=self.entry_a.pk)
        fresh_entry_a.set_current_language("en")
        fresh_entry_a.summary = "Updated Draft Summary A"
        fresh_entry_a.save()

        preview_html = self.client.get(preview_url(self.comparison.pk)).content.decode()
        self.assertIn("Updated Draft Summary A", preview_html)

        public_html = self.client.get("/en/compare/entrydraft-cmp/").content.decode()
        self.assertNotIn("Updated Draft Summary A", public_html)
        self.assertIn("Summary A", public_html)

    def test_republish_activates_the_draft_structure_publicly(self):
        reviewed = self._draft()
        fresh_entry_a = reviewed.tool_entries.get(pk=self.entry_a.pk)
        fresh_entry_a.tool = self.tool_c
        fresh_entry_a.save(update_fields=["tool"])

        publish(Comparison.objects.get(pk=self.comparison.pk), self.editor)
        public_html = self.client.get("/en/compare/entrydraft-cmp/").content.decode()
        self.assertIn("EntryDraft Tool C", public_html)
        self.assertNotIn("EntryDraft Tool A", public_html)


class FutureToolInPreviewTests(TestCase):
    """Group F: an authorized private preview may show a tool that is not
    yet public - the public page must not."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.editor = make_user("future-editor", group="Editor")
        self.client.force_login(self.editor)

        self.future_tool = make_tool(
            "futuretool-preview", "FutureTool Preview Tool", published_at=FUTURE
        )
        self.comparison = make_draft_comparison(
            self.editor, slug="futuretool-cmp", title="FutureTool Scenario"
        )
        add_entry(self.comparison, self.future_tool, position=10, summary="Future summary")
        self.comparison = publish(self.comparison, self.editor)

    def test_preview_shows_the_future_tool(self):
        html = self.client.get(preview_url(self.comparison.pk)).content.decode()
        self.assertIn("FutureTool Preview Tool", html)
        self.assertIn("Future summary", html)

    def test_public_page_does_not_show_the_future_tool(self):
        html = self.client.get("/en/compare/futuretool-cmp/").content.decode()
        self.assertNotIn("FutureTool Preview Tool", html)
        self.assertNotIn("Future summary", html)

    def test_preview_link_is_the_public_catalog_url_not_an_admin_url(self):
        html = self.client.get(preview_url(self.comparison.pk)).content.decode()
        expected_url = reverse("catalog:detail", kwargs={"slug": self.future_tool.slug})
        self.assertIn(expected_url, html)
        self.assertNotIn("/admin/catalog/", html)

    def test_no_error_and_no_unrelated_tools_leak(self):
        make_tool("unrelated-tool", "Unrelated Tool", published_at=PAST)
        resp = self.client.get(preview_url(self.comparison.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("Unrelated Tool", resp.content.decode())


class SnapshotMembershipInPreviewTests(TestCase):
    """Group G: the preview is a pure draft view - it is not gated by
    whether an entry is also a member of the published snapshot."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.editor = make_user("memb-editor", group="Editor")
        self.client.force_login(self.editor)

    def test_draft_only_entry_is_fully_visible_in_preview(self):
        tool = make_tool("memb-preview-tool", "Memb Preview Tool", published_at=PAST)
        comparison = make_draft_comparison(
            self.editor, slug="memb-preview-cmp", title="Memb Preview Scenario"
        )
        comparison = publish(comparison, self.editor)  # published with zero entries
        reviewed = start_review_round(comparison, self.editor)
        add_entry(reviewed, tool, position=10, summary="Draft-only summary")

        html = self.client.get(preview_url(comparison.pk)).content.decode()
        self.assertIn("Memb Preview Tool", html)
        self.assertIn("Draft-only summary", html)

        public_html = self.client.get("/en/compare/memb-preview-cmp/").content.decode()
        self.assertNotIn("Memb Preview Tool", public_html)


class EntryDeletedToolTests(TestCase):
    """A tool deleted after being referenced cascades the entry row away -
    no fallback or ghost entry is fabricated."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.editor = make_user("deltool-editor", group="Editor")
        self.client.force_login(self.editor)

    def test_deleted_tool_entry_is_absent_without_error(self):
        tool_a = make_tool("deltool-preview-a", "DelTool Preview A", published_at=PAST)
        tool_b = make_tool("deltool-preview-b", "DelTool Preview B", published_at=PAST)
        comparison = make_draft_comparison(
            self.editor, slug="deltool-preview-cmp", title="DelTool Preview Scenario"
        )
        add_entry(comparison, tool_a, position=10, summary="Summary A")
        add_entry(comparison, tool_b, position=20, summary="Summary B")
        comparison = publish(comparison, self.editor)

        tool_b.delete()

        resp = self.client.get(preview_url(comparison.pk))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("DelTool Preview A", html)
        self.assertNotIn("DelTool Preview B", html)
        self.assertNotIn("Summary B", html)


class EntryLanguageSafetyTests(TestCase):
    """Group E: entry translations are fail-closed per language, same as
    the public snapshot's per-entry language contract - no fallback in
    either direction."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.editor = make_user("entrylang-editor", group="Editor")
        self.client.force_login(self.editor)

    def test_en_only_entry_translation_shown_in_en_omitted_in_de(self):
        tool = make_tool("entrylang-tool-a", "EntryLang Tool A", published_at=PAST)
        comparison = make_draft_comparison(
            self.editor, slug="entrylang-cmp-en", title="EntryLang Scenario EN"
        )
        comparison.create_translation(
            "de", slug="entrylang-cmp-de", title="EntryLang Scenario DE", intro="i", body="b"
        )
        add_entry(
            comparison, tool, position=10, language="en", summary="EN-only entry summary"
        )
        comparison = publish(comparison, self.editor)

        html_en = self.client.get(preview_url(comparison.pk, "en")).content.decode()
        self.assertIn("EntryLang Tool A", html_en)
        self.assertIn("EN-only entry summary", html_en)

        html_de = self.client.get(preview_url(comparison.pk, "de")).content.decode()
        self.assertNotIn("EntryLang Tool A", html_de)
        self.assertNotIn("EN-only entry summary", html_de)

    def test_de_translation_shows_de_values_not_en(self):
        tool = make_tool("entrylang-tool-b", "EntryLang Tool B", published_at=PAST)
        comparison = make_draft_comparison(
            self.editor, slug="entrylang-both-cmp-en", title="EntryLang Both EN"
        )
        comparison.create_translation(
            "de", slug="entrylang-both-cmp-de", title="EntryLang Both DE", intro="i", body="b"
        )
        entry = add_entry(
            comparison, tool, position=10, language="en", summary="EN summary text"
        )
        entry.create_translation("de", label="", summary="DE summary text", pros="", cons="", special="")
        comparison = publish(comparison, self.editor)

        html_de = self.client.get(preview_url(comparison.pk, "de")).content.decode()
        self.assertIn("DE summary text", html_de)
        self.assertNotIn("EN summary text", html_de)

    def test_mixed_entries_only_explicitly_translated_ones_appear(self):
        tool_en_only = make_tool("entrylang-tool-enonly", "EntryLang EnOnly Tool", published_at=PAST)
        tool_both = make_tool("entrylang-tool-both", "EntryLang Both Tool", published_at=PAST)

        comparison = make_draft_comparison(
            self.editor, slug="entrylang-mixed-cmp-en", title="EntryLang Mixed EN"
        )
        comparison.create_translation(
            "de", slug="entrylang-mixed-cmp-de", title="EntryLang Mixed DE", intro="i", body="b"
        )
        add_entry(comparison, tool_en_only, position=10, language="en", summary="En only summary")
        entry_both = add_entry(comparison, tool_both, position=20, language="en", summary="Both en summary")
        entry_both.create_translation("de", label="", summary="Both de summary", pros="", cons="", special="")
        comparison = publish(comparison, self.editor)

        html_de = self.client.get(preview_url(comparison.pk, "de")).content.decode()
        self.assertNotIn("EntryLang EnOnly Tool", html_de)
        self.assertIn("EntryLang Both Tool", html_de)
        self.assertIn("Both de summary", html_de)
        self.assertNotIn("Both en summary", html_de)


class ToolNameLanguageFallbackTests(TestCase):
    """Beta 11.10B: ``draft_tool_entries()`` bulk-resolves each tool's
    display name via ``compare.presentation._resolve_draft_tools()`` instead
    of the lazy Parler field descriptor - these tests confirm that switch
    reproduces ``Tool.name``'s own language-then-configured-fallback chain
    byte-for-byte, and is entirely independent of the entry-level
    fail-closed language contract covered above (Group E)."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.editor = make_user("toollang-editor", group="Editor")
        self.client.force_login(self.editor)

    def test_en_only_tool_falls_back_to_en_name_under_de_preview(self):
        tool = make_tool("toollang-enonly", "ToolLang EnOnly Name", published_at=PAST)
        comparison = make_draft_comparison(
            self.editor, slug="toollang-enonly-cmp-en", title="ToolLang EnOnly EN"
        )
        comparison.create_translation(
            "de", slug="toollang-enonly-cmp-de", title="ToolLang EnOnly DE", intro="i", body="b"
        )
        entry = add_entry(comparison, tool, position=10, language="en", summary="En summary")
        entry.create_translation("de", label="", summary="De summary", pros="", cons="", special="")
        comparison = publish(comparison, self.editor)

        html_de = self.client.get(preview_url(comparison.pk, "de")).content.decode()
        self.assertIn("ToolLang EnOnly Name", html_de, "no DE tool translation exists - must fall back to EN")
        self.assertIn("De summary", html_de, "entry text is genuinely DE - unaffected by the tool-name fallback")

    def test_bilingual_tool_shows_own_language_name_not_the_fallback(self):
        tool = make_tool("toollang-bilingual", "ToolLang Bilingual EN Name", published_at=PAST)
        tool.create_translation("de", name="ToolLang Bilingual DE Name")
        comparison = make_draft_comparison(
            self.editor, slug="toollang-bilingual-cmp-en", title="ToolLang Bilingual EN"
        )
        comparison.create_translation(
            "de", slug="toollang-bilingual-cmp-de", title="ToolLang Bilingual DE", intro="i", body="b"
        )
        entry = add_entry(comparison, tool, position=10, language="en", summary="En summary")
        entry.create_translation("de", label="", summary="De summary", pros="", cons="", special="")
        comparison = publish(comparison, self.editor)

        html_de = self.client.get(preview_url(comparison.pk, "de")).content.decode()
        self.assertIn("ToolLang Bilingual DE Name", html_de)
        self.assertNotIn("ToolLang Bilingual EN Name", html_de)

        html_en = self.client.get(preview_url(comparison.pk, "en")).content.decode()
        self.assertIn("ToolLang Bilingual EN Name", html_en)
        self.assertNotIn("ToolLang Bilingual DE Name", html_en)

    def test_tool_with_no_translation_at_all_does_not_crash_the_preview(self):
        """Unreachable through normal ``TranslatableAdmin`` use (every saved
        Tool has at least the language tab it was saved with), but the lazy
        ``Tool.name`` field descriptor does raise ``Tool.DoesNotExist`` for
        this state - the bulk resolver deliberately does not: crashing the
        whole preview over one such row would be strictly worse for an
        editor than an empty name on that one card."""
        tool = Tool.objects.create(slug="toollang-notranslation", published_at=PAST)
        comparison = make_draft_comparison(
            self.editor, slug="toollang-notranslation-cmp", title="ToolLang NoTranslation"
        )
        add_entry(comparison, tool, position=10, language="en", summary="No-name summary")
        comparison = publish(comparison, self.editor)

        resp = self.client.get(preview_url(comparison.pk, "en"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("No-name summary", html)
        self.assertIn(reverse("catalog:detail", kwargs={"slug": "toollang-notranslation"}), html)
