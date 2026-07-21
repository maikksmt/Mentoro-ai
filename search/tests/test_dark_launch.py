"""
Beta 10.8: the search page is reachable but not advertised.

The page works under its own URL; nothing in the navigation, the footer or the
sitemap points at it yet. These tests guard both halves of that.
"""
import re
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from search.query import SearchQueryIssue
from search.tests.search_page_fixtures import empty_query_response


class DarkLaunchTestCase(TestCase):
    def setUp(self):
        self.addCleanup(translation.deactivate_all)


class DirectAccessTests(DarkLaunchTestCase):
    def test_both_language_pages_are_reachable(self):
        for path in ("/en/search/", "/de/search/"):
            with self.subTest(path=path):
                with patch("search.views.search_site") as service:
                    service.return_value = empty_query_response(SearchQueryIssue.EMPTY)
                    response = self.client.get(path)
                self.assertEqual(response.status_code, 200)

    def test_no_login_required(self):
        with patch("search.views.search_site") as service:
            service.return_value = empty_query_response(SearchQueryIssue.EMPTY)
            response = self.client.get("/en/search/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("login", response.get("Location", ""))


class NotLinkedTests(DarkLaunchTestCase):
    def _page(self, path="/en/"):
        return self.client.get(path, follow=True).content.decode()

    def test_homepage_does_not_link_to_the_search_page(self):
        html = self._page("/en/")
        self.assertNotIn('href="/en/search/"', html)

    def test_navigation_does_not_link_to_the_search_page(self):
        html = self._page("/en/")
        navigation = html[: html.index("</nav>") + 6] if "</nav>" in html else html
        self.assertNotIn("/en/search/", navigation)

    def test_footer_does_not_link_to_the_search_page(self):
        html = self._page("/en/")
        footer = html[html.index("<footer"):] if "<footer" in html else ""
        self.assertNotIn("/en/search/", footer)

    def test_search_placeholder_dialog_is_unchanged(self):
        html = self._page("/en/")
        dialog = re.search(r'<dialog id="searchmodal".*?</dialog>', html, re.DOTALL)
        self.assertIsNotNone(dialog)
        snippet = dialog.group(0)
        self.assertIn("Search is coming soon", snippet)
        self.assertNotIn("/en/search/", snippet)
        self.assertNotIn('name="q"', snippet)

    def test_content_pages_do_not_link_to_the_search_page(self):
        for path in ("/en/guides/", "/en/catalog/", "/en/compare/", "/en/prompts/"):
            with self.subTest(path=path):
                self.assertNotIn("/en/search/", self._page(path))

    def test_german_pages_do_not_link_to_the_search_page(self):
        self.assertNotIn("/de/search/", self._page("/de/"))


class SitemapTests(DarkLaunchTestCase):
    def test_sitemap_does_not_contain_the_search_url(self):
        for path in ("/en/sitemap.xml", "/de/sitemap.xml"):
            with self.subTest(path=path):
                body = self.client.get(path).content.decode()
                self.assertNotIn("/search/", body)

    def test_search_url_reverses_but_is_absent_from_the_sitemap(self):
        with translation.override("en"):
            url = reverse("search:results")
        self.assertEqual(url, "/en/search/")
        self.assertNotIn(url, self.client.get("/en/sitemap.xml").content.decode())
