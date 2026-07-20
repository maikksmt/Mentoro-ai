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


class RoadmapDialogAccessibleNameTests(TestCase):
    """Beta 9.9: the roadmap dialog gets an aria-labelledby association (it
    previously had a visible <h3> title but nothing connecting it to the
    dialog for assistive tech), plus a focus-return script mirroring the
    already-working search dialog pattern."""

    def _get_home_html(self):
        with translation.override("en"):
            resp = self.client.get("/", follow=True)
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_dialog_has_aria_labelledby(self):
        html = self._get_home_html()
        match = re.search(r'<dialog id="featuresmodal"[^>]*>', html)
        self.assertIsNotNone(match)
        self.assertIn('aria-labelledby="features-dialog-title"', match.group(0))

    def test_labelledby_target_id_exists_on_the_title(self):
        html = self._get_home_html()
        self.assertIn('id="features-dialog-title"', html)

    def test_close_button_has_autofocus(self):
        html = self._get_home_html()
        # Scope to the featuresmodal block so this can't accidentally match
        # the unrelated search dialog's own autofocus Close button.
        start = html.index('<dialog id="featuresmodal"')
        end = html.index("</dialog>", start)
        block = html[start:end]
        match = re.search(r'<button class="btn" autofocus>\s*Close\s*</button>', block)
        self.assertIsNotNone(match, "roadmap dialog Close button is missing autofocus")

    def test_focus_return_script_present(self):
        html = self._get_home_html()
        self.assertIn("getElementById('featuresmodal')", html)
        self.assertIn('querySelector(\'[aria-controls="featuresmodal"]\')', html)


class DecorativeIconTests(TestCase):
    """Beta 9.9: raw (non-heroicon) SVGs used purely as decoration next to
    an already-labelled control must not double-announce themselves to
    screen readers. Heroicon-rendered icons already carry aria-hidden by
    default (see the heroicons package's `_render_icon`) and aren't
    re-tested here."""

    def _get_home_html(self):
        with translation.override("en"):
            resp = self.client.get("/", follow=True)
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_search_icon_svg_is_hidden(self):
        html = self._get_home_html()
        # locate the opening <svg ...> tag that precedes this path's d= prefix
        start = html.index('d="M21 21l-6-6')
        svg_start = html.rindex("<svg", 0, start)
        svg_tag = html[svg_start:html.index(">", svg_start) + 1]
        self.assertIn('aria-hidden="true"', svg_tag)
        self.assertIn('focusable="false"', svg_tag)

    def test_theme_toggle_icons_are_hidden(self):
        html = self._get_home_html()
        for cls in ("swap-on", "swap-off"):
            with self.subTest(cls=cls):
                start = html.index(f'class="{cls}')
                svg_start = html.rindex("<svg", 0, start)
                svg_tag = html[svg_start:html.index(">", svg_start) + 1]
                self.assertIn('aria-hidden="true"', svg_tag)
                self.assertIn('focusable="false"', svg_tag)


class NoFalseButtonRoleOnPlainLinksTests(TestCase):
    """Beta 9.9: role="button"/aria-pressed were left over on ordinary
    navigating <a> links (header login/logout, glossary A-Z + pagination,
    starter guide card, author info card) - none of them are real toggle
    controls, so the false button semantics are removed. Real toggles
    (theme switch checkbox, mobile-nav trigger's aria-expanded) are
    untouched and not covered by this test."""

    def test_home_header_has_no_false_button_role(self):
        with translation.override("en"):
            resp = self.client.get("/", follow=True)
        html = resp.content.decode()
        self.assertNotIn('role="button"', html)
        self.assertNotIn("aria-pressed", html)

    def test_glossary_list_has_no_false_button_role(self):
        with translation.override("en"):
            resp = self.client.get("/en/glossary/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertNotIn('role="button"', html)
        self.assertNotIn("aria-pressed", html)
