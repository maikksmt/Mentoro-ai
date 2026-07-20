from pathlib import Path

from django.contrib.staticfiles import finders
from django.test import SimpleTestCase


class KlaroConfigTests(SimpleTestCase):
    """Beta 8.4: the theme-only preferences-cookies service/purpose is retired
    now that the theme lives in localStorage (Beta 8.3); other Klaro services
    and purposes must stay functionally untouched."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        path = finders.find("js/klaro/config.js")
        assert path, "static/js/klaro/config.js not found via staticfiles finders"
        cls.config_js = Path(path).read_text(encoding="utf-8")

    def test_theme_cookie_is_no_longer_listed(self):
        self.assertNotIn("preferred_theme", self.config_js)

    def test_preferences_service_is_removed(self):
        self.assertNotIn("preferences-cookies", self.config_js)

    def test_preferences_purpose_is_removed(self):
        self.assertNotIn("preferences: 'Preferences'", self.config_js)
        self.assertNotIn("preferences: 'Präferenzen'", self.config_js)

    def test_essential_cookies_service_remains_with_required_true(self):
        self.assertIn("name: 'essential-cookies'", self.config_js)
        start = self.config_js.index("name: 'essential-cookies'")
        end = self.config_js.index("name: 'google-analytics-cookies'")
        block = self.config_js[start:end]
        self.assertIn("required: true", block)
        self.assertIn("purposes: ['necessary']", block)
        self.assertIn("'csrftoken'", block)
        self.assertIn("'sessionid'", block)
        self.assertIn("'django_language'", block)
        self.assertIn("'htmx-current-path-for-history'", block)

    def test_google_analytics_service_remains_with_consent_callback(self):
        self.assertIn("name: 'google-analytics-cookies'", self.config_js)
        start = self.config_js.index("name: 'google-analytics-cookies'")
        block = self.config_js[start:]
        self.assertIn("purposes: ['statistics']", block)
        self.assertIn("function (consent)", block)
        self.assertIn("analytics_storage: consent ? 'granted' : 'denied'", block)
        self.assertIn("ad_storage: 'denied'", block)

    def test_only_two_services_remain(self):
        services_block = self.config_js[self.config_js.index("services: ["):]
        self.assertEqual(services_block.count("name: '"), 2)
