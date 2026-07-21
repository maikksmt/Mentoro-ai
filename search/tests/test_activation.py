"""
Beta 10.9: the search page is reachable *and* advertised.

Beta 10.8 shipped the page dark - working under its own URL, linked from
nowhere. This slice turns that around, so the tests that used to prove the
absence of links now prove their presence. What deliberately did not change:
the page stays out of the sitemap and stays noindex, because a result page is
still not worth indexing.
"""
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from search.query import SearchQueryIssue
from search.tests.search_page_fixtures import empty_query_response


class SearchActivationTestCase(TestCase):
    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)

    def page(self, path="/en/"):
        return self.client.get(path, follow=True).content.decode()


class DirectAccessTests(SearchActivationTestCase):
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


class LinkedFromEveryPageTests(SearchActivationTestCase):
    def test_homepage_links_to_the_search_page(self):
        self.assertIn('href="/en/search/"', self.page("/en/"))

    def test_link_lives_in_the_navigation(self):
        html = self.page("/en/")
        navigation = html[: html.index("</nav>") + 6] if "</nav>" in html else html
        self.assertIn("/en/search/", navigation)

    def test_content_pages_link_to_the_search_page(self):
        for path in ("/en/guides/", "/en/catalog/", "/en/compare/", "/en/prompts/"):
            with self.subTest(path=path):
                self.assertIn('href="/en/search/"', self.page(path))

    def test_german_pages_link_to_the_german_search_page(self):
        html = self.page("/de/")
        self.assertIn('href="/de/search/"', html)
        self.assertNotIn('href="/en/search/"', html)

    def test_the_link_appears_exactly_once(self):
        # Both the desktop and the mobile surface are served by the same
        # control in navbar-end. A second entry point would mean the mobile
        # dropdown grew its own copy.
        self.assertEqual(self.page("/en/").count('href="/en/search/"'), 1)

    def test_no_placeholder_dialog_remains(self):
        html = self.page("/en/")
        for remnant in (
            "searchmodal",
            "Search is coming soon",
            "search-dialog-title",
            'id="search-open"',
        ):
            with self.subTest(remnant=remnant):
                self.assertNotIn(remnant, html)


class SitemapTests(SearchActivationTestCase):
    def test_sitemap_still_does_not_contain_the_search_url(self):
        for path in ("/en/sitemap.xml", "/de/sitemap.xml"):
            with self.subTest(path=path):
                body = self.client.get(path).content.decode()
                self.assertNotIn("/search/", body)

    def test_search_url_reverses_but_is_absent_from_the_sitemap(self):
        with translation.override("en"):
            url = reverse("search:results")
        self.assertEqual(url, "/en/search/")
        self.assertNotIn(url, self.client.get("/en/sitemap.xml").content.decode())
