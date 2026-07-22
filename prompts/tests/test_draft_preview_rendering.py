"""
Beta 11.5 groups C/E: what the preview actually renders.

The preview must show the *saved draft* (title/intro/body/outro, including
the copyable prompt-text component) through the *real* public detail
template, while the public page keeps showing the published snapshot.
"""
from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from prompts.models import Prompt
from prompts.tests.draft_preview_fixtures import (
    make_draft_prompt,
    make_user,
    publish,
    save_translation_edit,
)


def preview_url(prompt_pk, language_code="en"):
    return reverse("admin:prompts_prompt_draft_preview", args=[prompt_pk, language_code])


class DraftVersusLiveTests(TestCase):
    """Group C: the published snapshot and the saved draft must not bleed
    into each other in either direction."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.editor = make_user("render-editor", group="Editor")
        self.client.force_login(self.editor)

    def test_never_published_draft_renders_its_saved_values(self):
        prompt = make_draft_prompt(
            self.editor,
            slug="pure-draft-en",
            title="Pure Draft Title",
            intro="Pure draft intro",
            body="<p>Pure draft body</p>",
            outro="<p>Pure draft outro</p>",
        )
        resp = self.client.get(preview_url(prompt.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Pure Draft Title")
        self.assertContains(resp, "Pure draft intro")
        self.assertContains(resp, "Pure draft body")
        self.assertContains(resp, "Pure draft outro")

    def test_preview_shows_draft_while_public_still_shows_the_snapshot(self):
        prompt = make_draft_prompt(
            self.editor,
            slug="diverged-en",
            title="Live Title",
            intro="Live intro",
            body="<p>Live body</p>",
            outro="<p>Live outro</p>",
        )
        publish(prompt, self.editor)
        save_translation_edit(
            prompt,
            "en",
            title="Draft Title",
            intro="Draft intro",
            body="<p>Draft body</p>",
            outro="<p>Draft outro</p>",
        )

        preview = self.client.get(preview_url(prompt.pk))
        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, "Draft Title")
        self.assertContains(preview, "Draft body")
        self.assertContains(preview, "Draft outro")
        self.assertNotContains(preview, "Live Title")
        self.assertNotContains(preview, "Live body")

        public = self.client.get("/en/prompts/diverged-en/")
        self.assertEqual(public.status_code, 200)
        self.assertContains(public, "Live Title")
        self.assertNotContains(public, "Draft Title")
        self.assertNotContains(public, "Draft body")

    def test_preview_does_not_read_the_live_snapshot(self):
        """A prompt whose snapshot and draft differ must render strictly the
        draft - proving get_display_value()/live_i18n are not the source."""
        prompt = make_draft_prompt(
            self.editor, slug="snapshot-check-en", title="Snapshot Title"
        )
        publish(prompt, self.editor)
        save_translation_edit(prompt, "en", title="Current Draft Title")

        refreshed = Prompt.objects.get(pk=prompt.pk)
        self.assertEqual(refreshed.live_i18n["en"]["title"], "Snapshot Title")
        self.assertEqual(refreshed.display_title, "Snapshot Title")

        resp = self.client.get(preview_url(prompt.pk))
        self.assertContains(resp, "Current Draft Title")
        self.assertNotContains(resp, "Snapshot Title")

    def test_copy_component_shows_the_draft_body(self):
        """The 'Copy prompt' block renders display_body - confirm it is the
        draft body, not the live snapshot."""
        prompt = make_draft_prompt(
            self.editor,
            slug="copy-diverged-en",
            title="Copy Title",
            body="<p>Live copy text</p>",
        )
        publish(prompt, self.editor)
        save_translation_edit(prompt, "en", body="<p>Draft copy text</p>")

        html = self.client.get(preview_url(prompt.pk)).content.decode()
        self.assertIn('data-copy-source', html)
        self.assertIn("Draft copy text", html)
        self.assertNotIn("Live copy text", html)


class TemplateParityTests(TestCase):
    """Group E: the preview goes through the real template and the real
    canonical richtext renderer."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.editor = make_user("parity-editor", group="Editor")
        self.client.force_login(self.editor)

    def test_preview_renders_the_real_public_detail_template(self):
        prompt = make_draft_prompt(self.editor, slug="parity-en", title="Parity Prompt")
        resp = self.client.get(preview_url(prompt.pk))
        self.assertTemplateUsed(resp, "prompts/prompt_detail.html")
        self.assertTemplateUsed(resp, "base.html")
        self.assertTemplateUsed(resp, "partials/breadcrumbs.html")
        self.assertTemplateUsed(resp, "partials/copy_block.html")

    def test_preview_shows_the_full_public_page_chrome(self):
        prompt = make_draft_prompt(self.editor, slug="chrome-en", title="Chrome Prompt")
        html = self.client.get(preview_url(prompt.pk)).content.decode()
        self.assertIn('class="navbar', html)
        self.assertIn('aria-label="Breadcrumbs"', html)
        self.assertIn('class="prose reading-column"', html)
        self.assertIn("<footer", html)
        self.assertIn('class="copy-block', html)

    def test_richtext_is_sanitized_through_the_canonical_renderer(self):
        prompt = make_draft_prompt(
            self.editor,
            slug="sanitize-en",
            title="Sanitize Prompt",
            body='<img src="x" onerror="alert(1)"><script>alert(2)</script><p>Safe body</p>',
        )
        html = self.client.get(preview_url(prompt.pk)).content.decode()
        self.assertNotIn("onerror", html)
        self.assertNotIn("<script>alert(2)</script>", html)
        self.assertIn("Safe body", html)

    def test_allowed_markup_is_not_double_escaped(self):
        prompt = make_draft_prompt(
            self.editor,
            slug="escape-en",
            title="Escape Prompt",
            body="<p><strong>Bold draft</strong></p>",
        )
        html = self.client.get(preview_url(prompt.pk)).content.decode()
        self.assertIn("<strong>Bold draft</strong>", html)
        self.assertNotIn("&lt;strong&gt;", html)

    def test_untranslated_relations_are_read_from_the_current_object(self):
        """tools/author/published_at/updated_at are not translated or
        snapshotted - the template reads them straight off the object,
        exactly as it does for the public page."""
        from catalog.models import Tool

        tool = Tool.objects.create(slug="preview-tool")
        tool.create_translation("en", name="Preview Tool")

        prompt = make_draft_prompt(
            self.editor, slug="tools-en", title="Tools Prompt"
        )
        prompt.tools.add(tool)

        html = self.client.get(preview_url(prompt.pk)).content.decode()
        self.assertIn("Preview Tool", html)
