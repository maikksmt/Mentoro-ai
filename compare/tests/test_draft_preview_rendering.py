"""
Beta 11.10 groups C/F/H: what the preview actually renders for the parent
comparison, template parity with the public page, and every workflow
status a saved comparison can be previewed from.
"""
from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin
from compare.tests.draft_preview_fixtures import (
    make_draft_comparison,
    make_user,
    publish,
    request_rework,
    save_translation_edit,
    start_review_round,
)


def preview_url(comparison_pk, language_code="en"):
    return reverse("admin:compare_comparison_draft_preview", args=[comparison_pk, language_code])


class DraftVersusLiveTests(TestCase):
    """Group C: the published snapshot and the saved draft must not bleed
    into each other in either direction."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.editor = make_user("render-editor", group="Editor")
        self.client.force_login(self.editor)

    def test_never_published_draft_renders_its_saved_values(self):
        comparison = make_draft_comparison(
            self.editor,
            slug="pure-draft-en",
            title="Pure Draft Title",
            intro="Pure draft intro",
            body="<p>Pure draft body</p>",
        )
        resp = self.client.get(preview_url(comparison.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Pure Draft Title")
        self.assertContains(resp, "Pure draft intro")
        self.assertContains(resp, "Pure draft body")

    def test_preview_shows_draft_while_public_still_shows_the_snapshot(self):
        comparison = make_draft_comparison(
            self.editor,
            slug="diverged-en",
            title="Live Title",
            intro="Live intro",
            body="<p>Live body</p>",
        )
        comparison = publish(comparison, self.editor)
        save_translation_edit(
            comparison, "en",
            title="Draft Title", intro="Draft intro", body="<p>Draft body</p>",
        )

        preview = self.client.get(preview_url(comparison.pk))
        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, "Draft Title")
        self.assertContains(preview, "Draft body")
        self.assertNotContains(preview, "Live Title")
        self.assertNotContains(preview, "Live body")

        public = self.client.get("/en/compare/diverged-en/")
        self.assertEqual(public.status_code, 200)
        self.assertContains(public, "Live Title")
        self.assertNotContains(public, "Draft Title")
        self.assertNotContains(public, "Draft body")

    def test_preview_does_not_read_the_live_snapshot(self):
        """A comparison whose snapshot and draft differ must render
        strictly the draft - proving get_display_value()/live_i18n are not
        the source."""
        comparison = make_draft_comparison(
            self.editor, slug="snapshot-check-en", title="Snapshot Title"
        )
        comparison = publish(comparison, self.editor)
        save_translation_edit(comparison, "en", title="Current Draft Title")

        refreshed = Comparison.objects.get(pk=comparison.pk)
        self.assertEqual(refreshed.live_i18n["en"]["title"], "Snapshot Title")
        self.assertEqual(refreshed.display_title, "Snapshot Title")

        resp = self.client.get(preview_url(comparison.pk))
        self.assertContains(resp, "Current Draft Title")
        self.assertNotContains(resp, "Snapshot Title")


class TemplateParityTests(TestCase):
    """Group F: the preview goes through the real template and the real
    canonical richtext renderer."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.editor = make_user("parity-editor", group="Editor")
        self.client.force_login(self.editor)

    def test_preview_renders_the_real_public_detail_template(self):
        comparison = make_draft_comparison(self.editor, slug="parity-en", title="Parity Comparison")
        resp = self.client.get(preview_url(comparison.pk))
        self.assertTemplateUsed(resp, "compare/comparison_detail.html")
        self.assertTemplateUsed(resp, "base.html")
        self.assertTemplateUsed(resp, "partials/breadcrumbs.html")

    def test_preview_shows_the_full_public_page_chrome(self):
        comparison = make_draft_comparison(self.editor, slug="chrome-en", title="Chrome Comparison")
        html = self.client.get(preview_url(comparison.pk)).content.decode()
        self.assertIn('class="navbar', html)
        self.assertIn('aria-label="Breadcrumbs"', html)
        self.assertIn('class="prose reading-column', html)
        self.assertIn("<footer", html)

    RICHTEXT_SAMPLE = (
        "<h2>Heading Two</h2>"
        "<h3>Heading Three</h3>"
        "<p>A paragraph with <strong>bold</strong>, <em>italic</em> and "
        '<code>inline code</code>. A <a href="https://example.com/">link</a>.</p>'
        "<ul><li>Bullet one</li><li>Bullet two</li></ul>"
        "<ol><li>Numbered one</li><li>Numbered two</li></ol>"
        "<blockquote><p>A quoted passage.</p></blockquote>"
        "<pre><code>def f():\n    return 1</code></pre>"
        '<table><thead><tr><th>Col</th></tr></thead>'
        "<tbody><tr><td>Cell value</td></tr></tbody></table>"
        '<img src="https://example.com/img.png" alt="Sample image">'
        "<hr>"
        '<div class="callout">A callout box.</div>'
    )

    def test_richtext_headings_paragraphs_and_inline_marks_render(self):
        comparison = make_draft_comparison(
            self.editor, slug="rt-inline-en", title="Richtext Inline", body=self.RICHTEXT_SAMPLE,
        )
        html = self.client.get(preview_url(comparison.pk)).content.decode()
        self.assertIn("<h2>Heading Two</h2>", html)
        self.assertIn("<h3>Heading Three</h3>", html)
        self.assertIn("<strong>bold</strong>", html)
        self.assertIn("<em>italic</em>", html)
        self.assertIn("<code>inline code</code>", html)
        self.assertIn('href="https://example.com/"', html)

    def test_richtext_lists_render(self):
        comparison = make_draft_comparison(
            self.editor, slug="rt-lists-en", title="Richtext Lists", body=self.RICHTEXT_SAMPLE,
        )
        html = self.client.get(preview_url(comparison.pk)).content.decode()
        self.assertIn("<ul>", html)
        self.assertIn("<li>Bullet one</li>", html)
        self.assertIn("<ol>", html)
        self.assertIn("<li>Numbered one</li>", html)

    def test_richtext_blockquote_and_code_block_render(self):
        comparison = make_draft_comparison(
            self.editor, slug="rt-block-en", title="Richtext Block", body=self.RICHTEXT_SAMPLE,
        )
        html = self.client.get(preview_url(comparison.pk)).content.decode()
        self.assertIn("<blockquote>", html)
        self.assertIn("A quoted passage.", html)
        self.assertIn("<pre><code>", html)

    def test_richtext_table_renders(self):
        comparison = make_draft_comparison(
            self.editor, slug="rt-table-en", title="Richtext Table", body=self.RICHTEXT_SAMPLE,
        )
        html = self.client.get(preview_url(comparison.pk)).content.decode()
        self.assertIn("<table>", html)
        self.assertIn("<th>Col</th>", html)
        self.assertIn("<td>Cell value</td>", html)

    def test_richtext_is_sanitized_through_the_canonical_renderer(self):
        comparison = make_draft_comparison(
            self.editor,
            slug="sanitize-en",
            title="Sanitize Comparison",
            body='<img src="x" onerror="alert(1)"><script>alert(2)</script><p>Safe body</p>',
        )
        html = self.client.get(preview_url(comparison.pk)).content.decode()
        self.assertNotIn("onerror", html)
        self.assertNotIn("<script>alert(2)</script>", html)
        self.assertIn("Safe body", html)

    def test_allowed_markup_is_not_double_escaped(self):
        comparison = make_draft_comparison(
            self.editor, slug="escape-en", title="Escape Comparison",
            body="<p><strong>Bold draft</strong></p>",
        )
        html = self.client.get(preview_url(comparison.pk)).content.decode()
        self.assertIn("<strong>Bold draft</strong>", html)
        self.assertNotIn("&lt;strong&gt;", html)


class WorkflowStatusTests(TestCase):
    """Group H: preview works for every real saved status; never mutates it."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.editor = make_user("workflow-editor", group="Editor")
        self.client.force_login(self.editor)

    def _assert_previewable_and_unchanged(self, comparison):
        before_status = Comparison.objects.get(pk=comparison.pk).status
        resp = self.client.get(preview_url(comparison.pk))
        self.assertEqual(resp.status_code, 200)
        after_status = Comparison.objects.get(pk=comparison.pk).status
        self.assertEqual(before_status, after_status)

    def test_draft_status_is_previewable(self):
        comparison = make_draft_comparison(self.editor, slug="wf-draft-en", title="WF Draft")
        self.assertEqual(comparison.status, EditorialWorkflowMixin.STATUS_DRAFT)
        self._assert_previewable_and_unchanged(comparison)

    def test_review_status_is_previewable(self):
        comparison = make_draft_comparison(self.editor, slug="wf-review-en", title="WF Review")
        comparison.move_to_review(by=self.editor)
        comparison.save()
        self._assert_previewable_and_unchanged(comparison)

    def test_approved_status_is_previewable(self):
        comparison = make_draft_comparison(self.editor, slug="wf-approved-en", title="WF Approved")
        comparison.move_to_review(by=self.editor)
        comparison.save()
        comparison.approve(by=self.editor)
        comparison.save()
        self._assert_previewable_and_unchanged(comparison)

    def test_published_status_is_previewable(self):
        comparison = make_draft_comparison(self.editor, slug="wf-published-en", title="WF Published")
        comparison = publish(comparison, self.editor)
        self._assert_previewable_and_unchanged(comparison)

    def test_rework_status_is_previewable(self):
        comparison = make_draft_comparison(self.editor, slug="wf-rework-en", title="WF Rework")
        comparison = publish(comparison, self.editor)
        comparison = start_review_round(comparison, self.editor)
        comparison = request_rework(comparison, self.editor)
        self._assert_previewable_and_unchanged(comparison)
