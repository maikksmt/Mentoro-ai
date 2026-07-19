import re

from django.test import TestCase
from django.utils import translation


class SkipLinkTests(TestCase):
    """Beta 8.5: a keyboard-only skip link jumps past header/nav to <main>."""

    def _get_home_html(self):
        with translation.override("en"):
            resp = self.client.get("/", follow=True)
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_skip_link_targets_main_content(self):
        html = self._get_home_html()
        self.assertIn('href="#main-content"', html)

    def test_skip_link_has_accessible_english_text(self):
        html = self._get_home_html()
        match = re.search(r'<a href="#main-content"[^>]*>\s*Skip to main content\s*</a>', html)
        self.assertIsNotNone(match, "skip link text not found")

    def test_skip_link_appears_before_the_navbar(self):
        html = self._get_home_html()
        skip_pos = html.index('href="#main-content"')
        navbar_pos = html.index('<div class="navbar')
        self.assertLess(skip_pos, navbar_pos)

    def test_main_content_id_exists_exactly_once(self):
        html = self._get_home_html()
        self.assertEqual(html.count('id="main-content"'), 1)

    def test_main_content_id_is_on_the_main_landmark(self):
        html = self._get_home_html()
        self.assertEqual(html.count("<main"), 1)
        match = re.search(r'<main[^>]*id="main-content"', html)
        self.assertIsNotNone(match, "id=main-content is not on the <main> element")

    def test_skip_link_is_not_hidden_from_assistive_tech(self):
        html = self._get_home_html()
        match = re.search(r'<a href="#main-content"[^>]*>', html)
        self.assertIsNotNone(match)
        tag = match.group(0)
        self.assertNotIn("display:none", tag.replace(" ", ""))
        self.assertNotIn("visibility:hidden", tag.replace(" ", ""))
        self.assertNotIn("hidden", tag)
        self.assertNotIn('aria-hidden="true"', tag)

    def test_skip_link_uses_focus_reveal_pattern(self):
        html = self._get_home_html()
        match = re.search(r'<a href="#main-content"\s+class="([^"]*)"', html)
        self.assertIsNotNone(match)
        classes = match.group(1)
        self.assertIn("sr-only", classes)
        self.assertIn("focus:not-sr-only", classes)


class RoadmapTriggerTests(TestCase):
    """Beta 8.5: the roadmap fab button gets an understandable accessible name."""

    def _get_home_html(self):
        with translation.override("en"):
            resp = self.client.get("/", follow=True)
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_trigger_has_new_accessible_label(self):
        html = self._get_home_html()
        self.assertIn('aria-label="View roadmap"', html)

    def test_old_generic_label_is_gone(self):
        html = self._get_home_html()
        self.assertNotIn("Information Button", html)

    def test_trigger_is_a_real_button(self):
        html = self._get_home_html()
        match = re.search(r'<button[^>]*aria-label="View roadmap"[^>]*>', html)
        self.assertIsNotNone(match, "roadmap trigger is not a <button> with the new label")

    def test_trigger_is_connected_to_the_dialog(self):
        html = self._get_home_html()
        match = re.search(r'<button[^>]*aria-label="View roadmap"[^>]*>', html)
        self.assertIsNotNone(match)
        tag = match.group(0)
        self.assertIn('aria-haspopup="dialog"', tag)
        self.assertIn('aria-controls="featuresmodal"', tag)

    def test_roadmap_dialog_still_present(self):
        html = self._get_home_html()
        self.assertIn('<dialog id="featuresmodal"', html)

    def test_trigger_still_opens_via_existing_onclick(self):
        html = self._get_home_html()
        self.assertIn('onclick="featuresmodal.showModal()"', html)
