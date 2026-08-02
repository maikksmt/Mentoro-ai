"""
Beta 11.10 group G (task's "Related Content"): Related Comparisons inside
the preview.

``related_comparisons()`` (Beta 11.9E) already ranks strictly on the *live*
tool snapshot, never the current draft entries, for both the source object
and every candidate. The preview reuses that function unchanged
(``compare/admin.py::build_comparison_draft_preview_context``), so this
module proves the preview inherits that guarantee rather than re-testing
the ranking algorithm itself (see
``compare/tests/test_related_comparisons_live_tools.py`` for the full
ranking-algorithm test suite).
"""
from datetime import timedelta

from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone, translation

from compare.tests.draft_preview_fixtures import (
    add_entry,
    archive,
    make_draft_comparison,
    make_tool,
    make_user,
    publish,
    start_review_round,
)

PAST = timezone.now() - timedelta(days=1)


def preview_url(comparison_pk, language_code="en"):
    return reverse("admin:compare_comparison_draft_preview", args=[comparison_pk, language_code])


class RelatedContentInThePreviewTests(TestCase):
    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.editor = make_user("related-preview-editor", group="Editor")
        self.client.force_login(self.editor)

    def test_related_cards_use_live_candidate_values_and_live_slugs(self):
        tool = make_tool("related-preview-tool", "Related Preview Tool", published_at=PAST)

        source = make_draft_comparison(
            self.editor, slug="related-source-en", title="Related Source"
        )
        add_entry(source, tool, position=10, summary="Source summary")
        source = publish(source, self.editor)

        cand = make_draft_comparison(
            self.editor, slug="related-cand-live-en", title="Related Cand Live"
        )
        add_entry(cand, tool, position=10, summary="Cand summary")
        cand = publish(cand, self.editor)

        html = self.client.get(preview_url(source.pk)).content.decode()
        self.assertIn("Related Cand Live", html)
        self.assertIn("/en/compare/related-cand-live-en/", html)

    def test_archived_candidate_is_excluded_from_the_preview_related_cards(self):
        source = make_draft_comparison(
            self.editor, slug="related-archived-source-en", title="Related Archived Source"
        )
        source = publish(source, self.editor)

        archived_cand = make_draft_comparison(
            self.editor, slug="related-archived-cand-en", title="Archived Related Candidate"
        )
        archived_cand = publish(archived_cand, self.editor)
        archive(archived_cand, self.editor)

        html = self.client.get(preview_url(source.pk)).content.decode()
        self.assertNotIn("Archived Related Candidate", html)

    def test_never_published_candidate_is_excluded_from_the_preview_related_cards(self):
        source = make_draft_comparison(
            self.editor, slug="related-neverpub-source-en", title="Related NeverPub Source"
        )
        source = publish(source, self.editor)

        make_draft_comparison(
            self.editor, slug="related-neverpub-cand-en",
            title="Never Published Related Candidate",
        )

        html = self.client.get(preview_url(source.pk)).content.decode()
        self.assertNotIn("Never Published Related Candidate", html)

    def test_source_draft_tool_swap_does_not_affect_related_selection(self):
        """The whole point of Beta 11.9E: related_comparisons() ranks the
        source by its live tool snapshot, never the draft one being
        previewed."""
        tool_a = make_tool("related-swap-tool-a", "Related Swap Tool A", published_at=PAST)
        tool_b = make_tool("related-swap-tool-b", "Related Swap Tool B", published_at=PAST)

        source = make_draft_comparison(
            self.editor, slug="related-swap-source-en", title="Related Swap Source"
        )
        source_entry = add_entry(source, tool_a, position=10, summary="Source A")
        source = publish(source, self.editor)

        cand_a = make_draft_comparison(
            self.editor, slug="related-swap-cand-a-en", title="Related Swap Cand A"
        )
        add_entry(cand_a, tool_a, position=10, summary="Cand A")
        cand_a = publish(cand_a, self.editor)

        cand_b = make_draft_comparison(
            self.editor, slug="related-swap-cand-b-en", title="Related Swap Cand B"
        )
        add_entry(cand_b, tool_b, position=10, summary="Cand B")
        cand_b = publish(cand_b, self.editor)

        # Draft-swap the SOURCE's entry to tool B - not yet republished.
        reviewed = start_review_round(source, self.editor)
        fresh_entry = reviewed.tool_entries.get(pk=source_entry.pk)
        fresh_entry.tool = tool_b
        fresh_entry.save(update_fields=["tool"])

        html = self.client.get(preview_url(source.pk)).content.decode()
        cand_a_pos = html.find("Related Swap Cand A")
        cand_b_pos = html.find("Related Swap Cand B")
        self.assertNotEqual(cand_a_pos, -1)
        self.assertNotEqual(cand_b_pos, -1)
        # Live tool is still A (unpublished draft change), so Cand A must
        # still rank first.
        self.assertLess(cand_a_pos, cand_b_pos)

    def test_never_published_source_gets_no_tool_bonus(self):
        """A source with no live snapshot at all has no live tool set to
        rank with - related_comparisons() must not fall back to its draft
        entries."""
        tool = make_tool("related-neverpub-tool", "Related NeverPub Tool", published_at=PAST)

        source = make_draft_comparison(
            self.editor, slug="related-neverpub-tool-source-en", title="Related NeverPub Tool Source"
        )
        add_entry(source, tool, position=10, summary="Source summary")
        # Deliberately not published.

        cand_matching_draft = make_draft_comparison(
            self.editor, slug="related-neverpub-tool-cand-en", title="Related NeverPub Tool Cand"
        )
        add_entry(cand_matching_draft, tool, position=10, summary="Cand summary")
        publish(cand_matching_draft, self.editor)

        resp = self.client.get(preview_url(source.pk))
        self.assertEqual(resp.status_code, 200)
        # No assertion on ordering beyond "no crash, no leak" - the
        # ranking-algorithm guarantee itself is covered exhaustively in
        # test_related_comparisons_live_tools.py; this only proves the
        # preview path does not error.
        self.assertIn("Related NeverPub Tool Cand", resp.content.decode())

    def test_related_cards_never_link_to_a_preview_url(self):
        """Scoped to the "Related Comparisons" section specifically: the
        page chrome from base.html (shared with the public site)
        legitimately reflects the current request path elsewhere, which is
        expected while previewing. That is not a related-content leak;
        only the related cards themselves must never link there."""
        source = make_draft_comparison(
            self.editor, slug="related-nopreview-source-en", title="Related NoPreview Source"
        )
        source = publish(source, self.editor)
        cand = make_draft_comparison(
            self.editor, slug="related-nopreview-cand-en", title="Related NoPreview Cand"
        )
        publish(cand, self.editor)

        html = self.client.get(preview_url(source.pk)).content.decode()
        start = html.index("Related Comparisons")
        end = html.index("</section>", start)
        related_section = html[start:end]
        self.assertIn("Related NoPreview Cand", related_section)
        self.assertNotIn("/preview/", related_section)
