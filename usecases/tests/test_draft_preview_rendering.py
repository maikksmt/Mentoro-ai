"""
Beta 11.8 groups C/F: what the preview actually renders.

The preview must show the *saved draft* (title/intro/body/outro, plus
``display_persona`` in the context - never rendered by the template today,
see ``usecases/presentation.py`` for why it is still carried through)
through the *real* public detail template, while the public page keeps
showing the published snapshot.
"""
from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from usecases.models import UseCase
from usecases.tests.draft_preview_fixtures import (
    make_draft_usecase,
    make_user,
    publish,
    save_translation_edit,
)


def preview_url(usecase_pk, language_code="en"):
    return reverse("admin:usecases_usecase_draft_preview", args=[usecase_pk, language_code])


class DraftVersusLiveTests(TestCase):
    """Group C: the published snapshot and the saved draft must not bleed
    into each other in either direction."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.editor = make_user("render-editor", group="Editor")
        self.client.force_login(self.editor)

    def test_never_published_draft_renders_its_saved_values(self):
        usecase = make_draft_usecase(
            self.editor,
            slug="pure-draft-en",
            title="Pure Draft Title",
            intro="Pure draft intro",
            body="<p>Pure draft body</p>",
            outro="<p>Pure draft outro</p>",
        )
        resp = self.client.get(preview_url(usecase.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Pure Draft Title")
        self.assertContains(resp, "Pure draft intro")
        self.assertContains(resp, "Pure draft body")
        self.assertContains(resp, "Pure draft outro")

    def test_preview_shows_draft_while_public_still_shows_the_snapshot(self):
        usecase = make_draft_usecase(
            self.editor,
            slug="diverged-en",
            title="Live Title",
            intro="Live intro",
            body="<p>Live body</p>",
            outro="<p>Live outro</p>",
        )
        publish(usecase, self.editor)
        save_translation_edit(
            usecase,
            "en",
            title="Draft Title",
            intro="Draft intro",
            body="<p>Draft body</p>",
            outro="<p>Draft outro</p>",
        )

        preview = self.client.get(preview_url(usecase.pk))
        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, "Draft Title")
        self.assertContains(preview, "Draft body")
        self.assertContains(preview, "Draft outro")
        self.assertNotContains(preview, "Live Title")
        self.assertNotContains(preview, "Live body")

        public = self.client.get("/en/usecases/diverged-en/")
        self.assertEqual(public.status_code, 200)
        self.assertContains(public, "Live Title")
        self.assertNotContains(public, "Draft Title")
        self.assertNotContains(public, "Draft body")

    def test_preview_does_not_read_the_live_snapshot(self):
        """A usecase whose snapshot and draft differ must render strictly
        the draft - proving get_display_value()/live_i18n are not the
        source."""
        usecase = make_draft_usecase(
            self.editor, slug="snapshot-check-en", title="Snapshot Title"
        )
        publish(usecase, self.editor)
        save_translation_edit(usecase, "en", title="Current Draft Title")

        refreshed = UseCase.objects.get(pk=usecase.pk)
        self.assertEqual(refreshed.live_i18n["en"]["title"], "Snapshot Title")
        self.assertEqual(refreshed.display_title, "Snapshot Title")

        resp = self.client.get(preview_url(usecase.pk))
        self.assertContains(resp, "Current Draft Title")
        self.assertNotContains(resp, "Snapshot Title")

    def test_preview_shows_draft_persona_in_context_while_public_shows_live(self):
        """persona is not currently rendered by templates/usecases/detail.html
        (confirmed empirically - no template change was made for it), but the
        public view's context includes display_persona unconditionally, and
        the preview mirrors that context shape. This asserts the *context*
        contract, not new visible output."""
        usecase = make_draft_usecase(
            self.editor, slug="persona-context-en", title="Persona Context",
            persona="Live Persona",
        )
        publish(usecase, self.editor)
        save_translation_edit(usecase, "en", persona="Draft Persona")

        preview_resp = self.client.get(preview_url(usecase.pk))
        self.assertEqual(preview_resp.context["display_persona"], "Draft Persona")

        public_resp = self.client.get("/en/usecases/persona-context-en/")
        self.assertEqual(public_resp.context["display_persona"], "Live Persona")

    def test_persona_is_not_rendered_visibly_by_either_page(self):
        """Guard against accidentally introducing new UI: neither preview nor
        public output should contain the persona text, since the template
        does not render it."""
        usecase = make_draft_usecase(
            self.editor, slug="persona-novisible-en", title="Persona NoVisible",
            persona="UNIQUEPERSONAMARKERXYZ",
        )
        html = self.client.get(preview_url(usecase.pk)).content.decode()
        self.assertNotIn("UNIQUEPERSONAMARKERXYZ", html)


class TemplateParityTests(TestCase):
    """Group F: the preview goes through the real template and the real
    canonical richtext renderer."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.editor = make_user("parity-editor", group="Editor")
        self.client.force_login(self.editor)

    def test_preview_renders_the_real_public_detail_template(self):
        usecase = make_draft_usecase(self.editor, slug="parity-en", title="Parity Usecase")
        resp = self.client.get(preview_url(usecase.pk))
        self.assertTemplateUsed(resp, "usecases/detail.html")
        self.assertTemplateUsed(resp, "base.html")
        self.assertTemplateUsed(resp, "partials/breadcrumbs.html")

    def test_preview_shows_the_full_public_page_chrome(self):
        usecase = make_draft_usecase(self.editor, slug="chrome-en", title="Chrome Usecase")
        html = self.client.get(preview_url(usecase.pk)).content.decode()
        self.assertIn('class="navbar', html)
        self.assertIn('aria-label="Breadcrumbs"', html)
        self.assertIn('class="prose reading-column"', html)
        self.assertIn("<footer", html)

    def test_untranslated_relations_are_read_from_the_current_object(self):
        """tools/author/published_at/updated_at are not translated or
        snapshotted - the template reads them straight off the object,
        exactly as it does for the public page."""
        from catalog.models import Tool

        tool = Tool.objects.create(slug="preview-tool")
        tool.create_translation("en", name="Preview Tool")

        usecase = make_draft_usecase(
            self.editor, slug="tools-en", title="Tools Usecase"
        )
        usecase.tools.add(tool)

        html = self.client.get(preview_url(usecase.pk)).content.decode()
        self.assertIn("Preview Tool", html)

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
        usecase = make_draft_usecase(
            self.editor, slug="rt-inline-en", title="Richtext Inline",
            body=self.RICHTEXT_SAMPLE,
        )
        html = self.client.get(preview_url(usecase.pk)).content.decode()
        self.assertIn("<h2>Heading Two</h2>", html)
        self.assertIn("<h3>Heading Three</h3>", html)
        self.assertIn("<strong>bold</strong>", html)
        self.assertIn("<em>italic</em>", html)
        self.assertIn("<code>inline code</code>", html)
        self.assertIn('href="https://example.com/"', html)

    def test_richtext_lists_render(self):
        usecase = make_draft_usecase(
            self.editor, slug="rt-lists-en", title="Richtext Lists",
            body=self.RICHTEXT_SAMPLE,
        )
        html = self.client.get(preview_url(usecase.pk)).content.decode()
        self.assertIn("<ul>", html)
        self.assertIn("<li>Bullet one</li>", html)
        self.assertIn("<ol>", html)
        self.assertIn("<li>Numbered one</li>", html)

    def test_richtext_blockquote_and_code_block_render(self):
        usecase = make_draft_usecase(
            self.editor, slug="rt-block-en", title="Richtext Block",
            body=self.RICHTEXT_SAMPLE,
        )
        html = self.client.get(preview_url(usecase.pk)).content.decode()
        self.assertIn("<blockquote>", html)
        self.assertIn("A quoted passage.", html)
        self.assertIn("<pre><code>", html)

    def test_richtext_table_renders(self):
        usecase = make_draft_usecase(
            self.editor, slug="rt-table-en", title="Richtext Table",
            body=self.RICHTEXT_SAMPLE,
        )
        html = self.client.get(preview_url(usecase.pk)).content.decode()
        self.assertIn("<table>", html)
        self.assertIn("<th>Col</th>", html)
        self.assertIn("<td>Cell value</td>", html)

    def test_richtext_image_and_hr_render(self):
        usecase = make_draft_usecase(
            self.editor, slug="rt-media-en", title="Richtext Media",
            body=self.RICHTEXT_SAMPLE,
        )
        html = self.client.get(preview_url(usecase.pk)).content.decode()
        self.assertIn('src="https://example.com/img.png"', html)
        self.assertIn("<hr>", html)

    def test_richtext_callout_div_renders(self):
        usecase = make_draft_usecase(
            self.editor, slug="rt-callout-en", title="Richtext Callout",
            body=self.RICHTEXT_SAMPLE,
        )
        html = self.client.get(preview_url(usecase.pk)).content.decode()
        self.assertIn("A callout box.", html)

    def test_richtext_is_sanitized_through_the_canonical_renderer(self):
        usecase = make_draft_usecase(
            self.editor,
            slug="sanitize-en",
            title="Sanitize Usecase",
            body='<img src="x" onerror="alert(1)"><script>alert(2)</script><p>Safe body</p>',
        )
        html = self.client.get(preview_url(usecase.pk)).content.decode()
        self.assertNotIn("onerror", html)
        self.assertNotIn("<script>alert(2)</script>", html)
        self.assertIn("Safe body", html)

    def test_allowed_markup_is_not_double_escaped(self):
        usecase = make_draft_usecase(
            self.editor,
            slug="escape-en",
            title="Escape Usecase",
            body="<p><strong>Bold draft</strong></p>",
        )
        html = self.client.get(preview_url(usecase.pk)).content.decode()
        self.assertIn("<strong>Bold draft</strong>", html)
        self.assertNotIn("&lt;strong&gt;", html)
