"""Beta 10.8: the search page reads correctly in both languages."""
from unittest.mock import patch

from django.test import TestCase
from django.utils import translation

from search.query import SearchQueryIssue
from search.result_types import SearchResultKind
from search.tests.search_page_fixtures import (
    empty_query_response,
    make_response,
    make_result,
    mixed_results,
)


class TranslationTestCase(TestCase):
    def setUp(self):
        self.addCleanup(translation.deactivate_all)

    def html(self, url, response):
        with patch("search.views.search_site") as service:
            service.return_value = response
            return self.client.get(url).content.decode()


class EnglishCopyTests(TranslationTestCase):
    def test_page_chrome(self):
        html = self.html(
            "/en/search/", empty_query_response(SearchQueryIssue.EMPTY)
        )
        self.assertIn("Search Mentoro AI", html)
        self.assertIn(
            "Find practical AI tools, guides, prompts, use cases, and comparisons.",
            html,
        )
        self.assertIn("What are you looking for?", html)
        self.assertIn("Enter at least 2 characters to search across Mentoro AI.", html)

    def test_validation_states(self):
        cases = (
            ("/en/search/?q=", SearchQueryIssue.EMPTY, "Enter a search term."),
            ("/en/search/?q=a", SearchQueryIssue.TOO_SHORT, "Enter at least 2 characters."),
            ("/en/search/?q=x", SearchQueryIssue.TOO_LONG, "Use no more than 100 characters."),
        )
        for url, issue, expected in cases:
            with self.subTest(issue=issue):
                html = self.html(url, empty_query_response(issue, value="a"))
                self.assertIn(expected, html)

    def test_result_and_empty_states(self):
        results_html = self.html("/en/search/?q=ai", make_response(results=mixed_results()))
        self.assertIn("Search results", results_html)
        self.assertIn("Results by type", results_html)
        self.assertIn("5 results for", results_html)

        empty_html = self.html(
            "/en/search/?q=nothing", make_response(value="nothing", results=())
        )
        self.assertIn("No results for", empty_html)
        self.assertIn("Try another search term or use fewer words.", empty_html)

    def test_type_labels(self):
        html = self.html("/en/search/?q=ai", make_response(results=mixed_results()))
        for label in ("Tools", "Guides", "Prompts", "Use cases", "Comparisons"):
            with self.subTest(label=label):
                self.assertIn(label, html)


class GermanCopyTests(TranslationTestCase):
    def test_page_chrome(self):
        html = self.html("/de/search/", empty_query_response(SearchQueryIssue.EMPTY))
        self.assertIn("Mentoro AI durchsuchen", html)
        self.assertIn(
            "Finde praktische KI-Tools, Leitfäden, Prompts, Anwendungsfälle und Vergleiche.",
            html,
        )
        self.assertIn("Was suchst Du?", html)
        self.assertIn("Gib mindestens 2 Zeichen ein, um Mentoro AI zu durchsuchen.", html)
        # The submit button reuses the platform's existing term for "Search"
        # ("Suche"), the same one the catalog and comparison search buttons
        # already use - a competing "Suchen" would read as a different action.
        self.assertIn("Suche", html)

    def test_validation_states(self):
        cases = (
            ("/de/search/?q=", SearchQueryIssue.EMPTY, "Gib einen Suchbegriff ein."),
            ("/de/search/?q=a", SearchQueryIssue.TOO_SHORT, "Gib mindestens 2 Zeichen ein."),
            ("/de/search/?q=x", SearchQueryIssue.TOO_LONG, "Verwende höchstens 100 Zeichen."),
        )
        for url, issue, expected in cases:
            with self.subTest(issue=issue):
                html = self.html(url, empty_query_response(issue, value="a"))
                self.assertIn(expected, html)

    def test_result_and_empty_states(self):
        results_html = self.html("/de/search/?q=KI", make_response(results=mixed_results()))
        self.assertIn("Suchergebnisse", results_html)
        self.assertIn("Ergebnisse nach Typ", results_html)
        self.assertIn("5 Ergebnisse für", results_html)

        empty_html = self.html(
            "/de/search/?q=nichts", make_response(value="nichts", results=())
        )
        self.assertIn("Keine Ergebnisse für", empty_html)
        self.assertIn(
            "Versuche einen anderen Suchbegriff oder verwende weniger Wörter.", empty_html
        )

    def test_singular_plural(self):
        singular = self.html(
            "/de/search/?q=KI",
            make_response(results=(make_result(SearchResultKind.TOOL, 1),)),
        )
        self.assertIn("1 Ergebnis für", singular)
        self.assertNotIn("1 Ergebnisse für", singular)

    def test_type_labels(self):
        html = self.html("/de/search/?q=KI", make_response(results=mixed_results()))
        for label in ("Tools", "Leitfäden", "Prompts", "Anwendungsfälle", "Vergleiche"):
            with self.subTest(label=label):
                self.assertIn(label, html)
        for label in ("Leitfaden", "Anwendungsfall", "Vergleich"):
            with self.subTest(badge=label):
                self.assertIn(label, html)

    def test_error_state(self):
        from search.exceptions import SearchExecutionError

        with patch("search.views.search_site") as service:
            service.side_effect = SearchExecutionError(
                SearchResultKind.GUIDE, "adapter raised"
            )
            html = self.client.get("/de/search/?q=KI").content.decode()
        self.assertIn("Die Suche ist vorübergehend nicht verfügbar.", html)
        self.assertIn("Versuche es gleich noch einmal.", html)

    def test_uses_informal_address_only(self):
        pages = [
            self.html("/de/search/", empty_query_response(SearchQueryIssue.EMPTY)),
            self.html("/de/search/?q=KI", make_response(results=mixed_results())),
            self.html("/de/search/?q=x", make_response(value="x", results=())),
        ]
        for index, html in enumerate(pages):
            body = html[html.index("<main") if "<main" in html else 0:]
            for formal in ("Sie suchen", "Ihre Suche", "Geben Sie", "Versuchen Sie"):
                with self.subTest(page=index, formal=formal):
                    self.assertNotIn(formal, body)

    def test_no_untranslated_english_search_copy_leaks_into_german(self):
        html = self.html("/de/search/?q=KI", make_response(results=mixed_results()))
        for english_only in (
            "Search Mentoro AI",
            "Search results",
            "Results by type",
            "What are you looking for?",
        ):
            with self.subTest(string=english_only):
                self.assertNotIn(english_only, html)
