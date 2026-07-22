"""
Beta 11.4 groups C/E/F: what the preview actually renders.

The preview must show the *saved draft* through the *real* public detail
template, while the public page keeps showing the published snapshot.
"""
from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from guides.models import Guide
from guides.tests.draft_preview_fixtures import (
    add_item,
    add_section,
    keep_publicly_visible,
    make_draft_guide,
    make_user,
    publish,
    save_translation_edit,
)


def preview_url(guide_pk, language_code="en"):
    return reverse("admin:guides_guide_draft_preview", args=[guide_pk, language_code])


class DraftVersusLiveTests(TestCase):
    """Group C: the published snapshot and the saved draft must not bleed
    into each other in either direction."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.editor = make_user("render-editor", group="Editor")
        self.client.force_login(self.editor)

    def test_never_published_draft_renders_its_saved_values(self):
        guide = make_draft_guide(
            self.editor,
            slug="pure-draft-en",
            title="Pure Draft Title",
            intro="Pure draft intro",
            body="<p>Pure draft body</p>",
        )
        resp = self.client.get(preview_url(guide.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Pure Draft Title")
        self.assertContains(resp, "Pure draft intro")
        self.assertContains(resp, "Pure draft body")

    def test_preview_shows_draft_while_public_still_shows_the_snapshot(self):
        guide = make_draft_guide(
            self.editor,
            slug="diverged-en",
            title="Live Title",
            intro="Live intro",
            body="<p>Live body</p>",
        )
        publish(guide, self.editor)
        save_translation_edit(
            guide,
            "en",
            title="Draft Title",
            intro="Draft intro",
            body="<p>Draft body</p>",
        )

        preview = self.client.get(preview_url(guide.pk))
        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, "Draft Title")
        self.assertContains(preview, "Draft body")
        self.assertNotContains(preview, "Live Title")

        public = self.client.get("/en/guides/diverged-en/")
        self.assertEqual(public.status_code, 200)
        self.assertContains(public, "Live Title")
        self.assertNotContains(public, "Draft Title")
        self.assertNotContains(public, "Draft body")

    def test_preview_does_not_read_the_live_snapshot(self):
        """A guide whose snapshot and draft differ must render strictly the
        draft - proving get_display_value()/live_i18n are not the source."""
        guide = make_draft_guide(
            self.editor, slug="snapshot-check-en", title="Snapshot Title"
        )
        publish(guide, self.editor)
        save_translation_edit(guide, "en", title="Current Draft Title")

        refreshed = Guide.objects.get(pk=guide.pk)
        self.assertEqual(refreshed.live_i18n["en"]["title"], "Snapshot Title")
        self.assertEqual(refreshed.display_title, "Snapshot Title")

        resp = self.client.get(preview_url(guide.pk))
        self.assertContains(resp, "Current Draft Title")
        self.assertNotContains(resp, "Snapshot Title")


class DraftSectionAndItemTests(TestCase):
    """Group E: sections and items come from the saved draft, in the saved
    order, with the public is_published gate intact."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.editor = make_user("section-editor", group="Editor")
        self.client.force_login(self.editor)

    def test_saved_draft_sections_are_rendered(self):
        guide = make_draft_guide(
            self.editor, slug="with-sections-en", title="Sectioned Guide"
        )
        add_section(
            guide,
            order=0,
            translations={"en": ("First Section", "<p>First section body</p>")},
        )
        resp = self.client.get(preview_url(guide.pk))
        self.assertContains(resp, "First Section")
        self.assertContains(resp, "First section body")

    def test_sections_keep_their_saved_order(self):
        guide = make_draft_guide(
            self.editor, slug="ordered-sections-en", title="Ordered Guide"
        )
        add_section(guide, order=0, translations={"en": ("Alpha Section", "a")})
        add_section(guide, order=1, translations={"en": ("Beta Section", "b")})
        add_section(guide, order=2, translations={"en": ("Gamma Section", "g")})

        html = self.client.get(preview_url(guide.pk)).content.decode()
        self.assertLess(html.index("Alpha Section"), html.index("Beta Section"))
        self.assertLess(html.index("Beta Section"), html.index("Gamma Section"))

    def test_section_draft_edit_is_shown_while_public_keeps_the_snapshot(self):
        guide = make_draft_guide(
            self.editor, slug="section-diverged-en", title="Guide With Section"
        )
        section = add_section(
            guide, order=0, translations={"en": ("Live Section", "<p>Live sec body</p>")}
        )
        publish(guide, self.editor)
        save_translation_edit(
            section, "en", title="Draft Section", body="<p>Draft sec body</p>"
        )
        # Editing a section knocks the guide back to review (guides/signals.py);
        # a live revision keeps it on the public site.
        keep_publicly_visible(Guide.objects.get(pk=guide.pk))

        preview = self.client.get(preview_url(guide.pk))
        self.assertContains(preview, "Draft Section")
        self.assertContains(preview, "Draft sec body")
        self.assertNotContains(preview, "Live Section")

        public = self.client.get("/en/guides/section-diverged-en/")
        self.assertEqual(public.status_code, 200)
        self.assertContains(public, "Live Section")
        self.assertNotContains(public, "Draft Section")

    def test_guide_items_render_their_saved_draft_values(self):
        guide = make_draft_guide(self.editor, slug="with-items-en", title="Item Guide")
        section = add_section(guide, order=0, translations={"en": ("Sec", "b")})
        add_item(
            section,
            order=0,
            translations={"en": ("Draft Item Title", "Draft item teaser")},
        )
        resp = self.client.get(preview_url(guide.pk))
        self.assertContains(resp, "Draft Item Title")
        self.assertContains(resp, "Draft item teaser")

    def test_unpublished_items_stay_hidden_exactly_as_on_the_public_page(self):
        guide = make_draft_guide(
            self.editor, slug="hidden-item-en", title="Retracted Card Guide"
        )
        section = add_section(guide, order=0, translations={"en": ("Sec", "b")})
        hidden = add_item(
            section, order=0, translations={"en": ("Suppressed Item", "teaser")}
        )
        hidden.is_published = False
        hidden.save()

        resp = self.client.get(preview_url(guide.pk))
        self.assertNotContains(resp, "Suppressed Item")


class TemplateParityTests(TestCase):
    """Group F: the preview goes through the real template and the real
    canonical richtext renderer."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.editor = make_user("parity-editor", group="Editor")
        self.client.force_login(self.editor)

    def test_preview_renders_the_real_public_detail_template(self):
        guide = make_draft_guide(self.editor, slug="parity-en", title="Parity Guide")
        resp = self.client.get(preview_url(guide.pk))
        self.assertTemplateUsed(resp, "guides/guide_detail.html")
        self.assertTemplateUsed(resp, "base.html")
        self.assertTemplateUsed(resp, "partials/breadcrumbs.html")

    def test_preview_shows_the_full_public_page_chrome(self):
        guide = make_draft_guide(self.editor, slug="chrome-en", title="Chrome Guide")
        html = self.client.get(preview_url(guide.pk)).content.decode()
        # Navigation, breadcrumbs and the reading column the public page uses.
        self.assertIn('class="navbar', html)
        self.assertIn('aria-label="Breadcrumbs"', html)
        self.assertIn('class="prose reading-column"', html)
        self.assertIn("<footer", html)

    def test_richtext_is_sanitized_through_the_canonical_renderer(self):
        guide = make_draft_guide(
            self.editor,
            slug="sanitize-en",
            title="Sanitize Guide",
            body='<img src="x" onerror="alert(1)"><script>alert(2)</script><p>Safe body</p>',
        )
        html = self.client.get(preview_url(guide.pk)).content.decode()
        self.assertNotIn("onerror", html)
        self.assertNotIn("<script>alert(2)</script>", html)
        self.assertIn("Safe body", html)

    def test_allowed_markup_is_not_double_escaped(self):
        guide = make_draft_guide(
            self.editor,
            slug="escape-en",
            title="Escape Guide",
            body="<p><strong>Bold draft</strong></p>",
        )
        html = self.client.get(preview_url(guide.pk)).content.decode()
        self.assertIn("<strong>Bold draft</strong>", html)
        self.assertNotIn("&lt;strong&gt;", html)
