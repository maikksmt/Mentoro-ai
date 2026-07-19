import re

from django.test import TestCase
from django.utils import translation


class SearchPlaceholderDialogTests(TestCase):
    """Covers Beta 8.1: the global search button stays a placeholder that
    opens an honest, accessible "coming soon" dialog instead of searching."""

    def _get_home_html(self) -> str:
        with translation.override("en"):
            resp = self.client.get("/", follow=True)
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def _dialog_snippet(self, html: str) -> str:
        match = re.search(r'<dialog id="searchmodal".*?</dialog>', html, re.DOTALL)
        self.assertIsNotNone(match, "search dialog not found in rendered HTML")
        return match.group(0)

    def test_search_button_has_accessible_label_and_dialog_relationship(self):
        html = self._get_home_html()
        match = re.search(r'<button[^>]*id="search-open"[^>]*>', html)
        self.assertIsNotNone(match, "search button not found")
        button_tag = match.group(0)
        self.assertIn('aria-controls="searchmodal"', button_tag)
        self.assertIn("aria-label=", button_tag)

    def test_search_dialog_is_present_with_translated_copy(self):
        html = self._get_home_html()
        dialog = self._dialog_snippet(html)
        self.assertIn("Search is coming soon", dialog)
        self.assertIn(
            "unified search across tools, guides, prompts, use cases, and comparisons.",
            dialog,
        )

    def test_search_dialog_has_accessible_labelling(self):
        html = self._get_home_html()
        dialog = self._dialog_snippet(html)
        self.assertIn('aria-labelledby="search-dialog-title"', dialog)
        self.assertIn('aria-describedby="search-dialog-desc"', dialog)
        self.assertIn('id="search-dialog-title"', dialog)
        self.assertIn('id="search-dialog-desc"', dialog)

    def test_search_dialog_has_a_visible_close_action(self):
        html = self._get_home_html()
        dialog = self._dialog_snippet(html)
        self.assertIn(">Close<", dialog)

    def test_search_dialog_contains_no_search_field_or_search_route(self):
        html = self._get_home_html()
        dialog = self._dialog_snippet(html)
        self.assertNotIn("<input", dialog)
        self.assertNotIn('type="search"', dialog)
        self.assertNotIn("name=\"q\"", dialog)

    def test_features_roadmap_dialog_still_present(self):
        html = self._get_home_html()
        self.assertIn('id="featuresmodal"', html)
