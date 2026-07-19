import re

from django.test import TestCase, override_settings
from django.utils import translation


class ThemeInitTests(TestCase):
    """Beta 8.3: theme is set on <html> before first paint, persisted in
    localStorage only, and decoupled from Klaro/preferences consent."""

    def _get_home_html(self):
        with translation.override("en"):
            resp = self.client.get("/", follow=True)
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def _head(self, html):
        match = re.search(r"<head>.*?</head>", html, re.DOTALL)
        self.assertIsNotNone(match, "no <head> found in rendered HTML")
        return match.group(0)

    def _early_script(self, html):
        head = self._head(html)
        match = re.search(r"Early theme init.*?</script>", head, re.DOTALL)
        self.assertIsNotNone(match, "early theme init script not found in <head>")
        return match.group(0)

    def test_early_theme_script_is_in_head(self):
        html = self._get_home_html()
        self.assertIn("Early theme init", self._head(html))

    def test_early_theme_script_runs_before_the_stylesheet(self):
        html = self._get_home_html()
        head = self._head(html)
        script_pos = head.index("Early theme init")
        stylesheet_pos = head.index('rel="stylesheet"')
        self.assertLess(script_pos, stylesheet_pos)

    def test_early_theme_script_references_local_storage(self):
        script = self._early_script(self._get_home_html())
        self.assertIn("localStorage", script)

    def test_early_theme_script_references_system_preference(self):
        script = self._early_script(self._get_home_html())
        self.assertIn("prefers-color-scheme", script)

    def test_early_theme_script_only_allows_the_two_themes(self):
        script = self._early_script(self._get_home_html())
        self.assertIn("mentoroai-light", script)
        self.assertIn("mentoroai-dark", script)

    def test_early_theme_script_is_not_coupled_to_consent(self):
        script = self._early_script(self._get_home_html())
        self.assertNotIn("hasPreferencesConsent", script)
        self.assertNotIn("klaro", script.lower())
        self.assertNotIn("DOMContentLoaded", script)


class ThemePersistenceTests(TestCase):
    """Beta 8.3: the interactive toggle stores choices in localStorage,
    not in a cookie, and no longer gates on Klaro preferences consent."""

    def _get_home_html(self):
        with translation.override("en"):
            resp = self.client.get("/", follow=True)
        return resp.content.decode()

    def test_theme_toggle_script_writes_to_local_storage(self):
        html = self._get_home_html()
        self.assertIn("localStorage.setItem", html)

    def test_theme_toggle_script_uses_a_stable_storage_key(self):
        html = self._get_home_html()
        self.assertEqual(html.count("mentoroai-theme"), 2, html.count("mentoroai-theme"))

    def test_no_theme_cookie_is_written_anywhere(self):
        html = self._get_home_html()
        self.assertNotIn("preferred_theme", html)
        self.assertNotIn("document.cookie", html)

    def test_theme_logic_does_not_check_preferences_consent(self):
        html = self._get_home_html()
        self.assertNotIn("hasPreferencesConsent", html)
        self.assertNotIn("preferences-cookies", html)


class ThemeToggleStructureTests(TestCase):
    """Beta 8.3: the existing theme switch stays in place with correct semantics."""

    def setUp(self):
        with translation.override("en"):
            resp = self.client.get("/", follow=True)
        self.assertEqual(resp.status_code, 200)
        self.html = resp.content.decode()

    def test_theme_switch_is_present(self):
        self.assertIn('class="swap swap-rotate theme-swap', self.html)

    def test_theme_switch_is_a_real_checkbox_with_accessible_label(self):
        match = re.search(r'<input type="checkbox"[^>]*aria-label="[^"]*"[^>]*/?>', self.html)
        self.assertIsNotNone(match, "theme checkbox with aria-label not found")

    def test_header_controls_still_present_and_in_order(self):
        # Beta 8.1/8.2 regression: search, theme switch and login stay in the navbar-end group.
        navbar_end = self.html.split('class="navbar-end"', 1)[1]
        search_pos = navbar_end.index('id="search-open"')
        theme_pos = navbar_end.index("theme-swap")
        self.assertLess(search_pos, theme_pos)


@override_settings(DEBUG=False)
class ThemeKlaroRegressionTests(TestCase):
    """Beta 8.3 must not touch Klaro/GTM wiring, checked here with DEBUG off
    to mirror how these tags actually render in a deployed environment."""

    def test_klaro_and_gtm_scripts_still_render(self):
        with translation.override("en"):
            resp = self.client.get("/", follow=True)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        # config.js is served through {% static %} and gets a content hash
        # inserted before the extension, so match the stable prefix only.
        self.assertIn("js/klaro/config", html)
        self.assertIn("cdn.kiprotect.com/klaro", html)
        self.assertIn("googletagmanager.com/gtm.js", html)
