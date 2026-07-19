from django.test import TestCase
from django.urls import reverse
from django.utils import translation


class CookiePolicyThemeSectionTests(TestCase):
    """Beta 8.4: the cookie policy must describe the theme choice as a
    localStorage entry, not as a cookie (Beta 8.3 moved it out of cookies)."""

    def _get_html(self):
        with translation.override("en"):
            resp = self.client.get(reverse("legal:cookies"))
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_mentions_local_storage(self):
        self.assertIn("localStorage", self._get_html())

    def test_mentions_storage_key(self):
        self.assertIn("mentoroai-theme", self._get_html())

    def test_no_longer_mentions_old_theme_cookie(self):
        self.assertNotIn("preferred_theme", self._get_html())

    def test_theme_is_not_described_as_a_cookie(self):
        self.assertIn("rather than a cookie", self._get_html())

    def test_no_fixed_expiry_claimed_for_theme_entry(self):
        html = self._get_html()
        # Analytics cookies document a lifetime ("expires after ..."); the
        # theme entry must not carry the same kind of claim.
        theme_section = html.split("Local theme preference", 1)[1].split("Statistics cookies", 1)[0]
        self.assertNotIn("expires", theme_section)

    def test_other_main_sections_still_present(self):
        html = self._get_html()
        self.assertIn("Technically necessary cookies", html)
        self.assertIn("Statistics cookies (Google Analytics 4)", html)
        self.assertIn("Cookie control", html)


class PrivacyPolicyThemeSectionTests(TestCase):
    """Beta 8.4: the privacy policy must not describe the theme choice as a
    server-transmitted cookie."""

    def _get_html(self):
        with translation.override("en"):
            resp = self.client.get(reverse("legal:privacy"))
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_mentions_local_storage(self):
        self.assertIn("localStorage", self._get_html())

    def test_mentions_storage_key(self):
        self.assertIn("mentoroai-theme", self._get_html())

    def test_no_longer_mentions_old_theme_cookie(self):
        self.assertNotIn("preferred_theme", self._get_html())

    def test_does_not_claim_theme_is_transmitted(self):
        self.assertIn("not transmitted to us", self._get_html())

    def test_other_main_sections_still_present(self):
        html = self._get_html()
        self.assertIn("Cookies, tracking and consent management", html)
        self.assertIn("Consent management (Klaro)", html)
        self.assertIn("Google Tag Manager", html)
