"""
Beta 10.8: the search page's own behaviour, with the service patched out.

Patching at the view's import boundary keeps these tests about presentation:
which state is rendered, what reaches the service, and what happens when it
fails. The service's own correctness is covered by its own tests.
"""
from unittest.mock import patch

from django.test import TestCase
from django.utils import translation

from search.exceptions import SearchExecutionError
from search.query import SearchQueryIssue
from search.result_types import SearchResultKind
from search.tests.search_page_fixtures import (
    empty_query_response,
    make_response,
    make_result,
    mixed_results,
)

SEARCH_URL = "/en/search/"


class SearchViewTestCase(TestCase):
    def setUp(self):
        # LocaleMiddleware activates a language per request and never restores
        # it; without this, a German request here would leak into unrelated
        # tests that assert on English copy.
        self.addCleanup(translation.deactivate_all)

    def get(self, url=SEARCH_URL, response=None, side_effect=None):
        with patch("search.views.search_site") as service:
            if side_effect is not None:
                service.side_effect = side_effect
            else:
                service.return_value = response or make_response()
            http_response = self.client.get(url)
        return http_response, service


class ServiceBoundaryTests(SearchViewTestCase):
    def test_service_is_called_exactly_once_with_keyword_arguments(self):
        _, service = self.get("/en/search/?q=ai%20tools")
        service.assert_called_once_with(raw_query="ai tools", language_code="en")

    def test_missing_parameter_passes_none(self):
        _, service = self.get("/en/search/")
        service.assert_called_once_with(raw_query=None, language_code="en")

    def test_empty_parameter_passes_empty_string(self):
        _, service = self.get("/en/search/?q=")
        service.assert_called_once_with(raw_query="", language_code="en")

    def test_language_comes_from_the_url_prefix(self):
        _, service = self.get("/de/search/?q=KI")
        service.assert_called_once_with(raw_query="KI", language_code="de")

    def test_ambient_language_does_not_override_the_request_language(self):
        with translation.override("en"):
            _, service = self.get("/de/search/?q=KI")
        self.assertEqual(service.call_args.kwargs["language_code"], "de")

    def test_unknown_parameters_are_ignored(self):
        _, service = self.get("/en/search/?q=ai%20tools&type=tool&page=3&sort=date")
        service.assert_called_once_with(raw_query="ai tools", language_code="en")

    def test_post_is_rejected_without_calling_the_service(self):
        with patch("search.views.search_site") as service:
            response = self.client.post(SEARCH_URL, {"q": "ai tools"})
        self.assertEqual(response.status_code, 405)
        service.assert_not_called()


class InitialStateTests(SearchViewTestCase):
    def test_initial_visit_shows_the_neutral_hint(self):
        response, _ = self.get(
            "/en/search/", response=empty_query_response(SearchQueryIssue.EMPTY)
        )
        html = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Enter at least 2 characters to search across Mentoro AI.", html)
        self.assertNotIn("Enter a search term.", html)
        self.assertNotIn("Search results", html)

    def test_initial_visit_shows_no_counts(self):
        response, _ = self.get(
            "/en/search/", response=empty_query_response(SearchQueryIssue.EMPTY)
        )
        self.assertNotIn("Results by type", response.content.decode())


class InvalidQueryStateTests(SearchViewTestCase):
    def test_explicitly_empty_query_differs_from_the_initial_visit(self):
        response, _ = self.get(
            "/en/search/?q=", response=empty_query_response(SearchQueryIssue.EMPTY)
        )
        html = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Enter a search term.", html)
        self.assertNotIn("Enter at least 2 characters to search across", html)

    def test_whitespace_only_query_is_treated_as_empty(self):
        response, _ = self.get(
            "/en/search/?q=%20%20%20",
            response=empty_query_response(SearchQueryIssue.EMPTY),
        )
        self.assertIn("Enter a search term.", response.content.decode())

    def test_too_short_query(self):
        response, _ = self.get(
            "/en/search/?q=a",
            response=empty_query_response(SearchQueryIssue.TOO_SHORT, value="a"),
        )
        html = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Enter at least 2 characters.", html)
        self.assertIn('value="a"', html)

    def test_too_long_query_is_not_silently_truncated(self):
        long_value = "x" * 120
        response, _ = self.get(
            f"/en/search/?q={long_value}",
            response=empty_query_response(SearchQueryIssue.TOO_LONG, value=long_value),
        )
        html = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Use no more than 100 characters.", html)
        self.assertIn(f'value="{long_value}"', html)

    def test_invalid_states_render_no_results_and_no_counts(self):
        for issue in SearchQueryIssue:
            with self.subTest(issue=issue):
                response, _ = self.get(
                    "/en/search/?q=a", response=empty_query_response(issue, value="a")
                )
                html = response.content.decode()
                self.assertNotIn("Search results", html)
                self.assertNotIn("Results by type", html)


class ResultStateTests(SearchViewTestCase):
    def test_results_render_in_service_order(self):
        response, _ = self.get(
            "/en/search/?q=ai%20tools",
            response=make_response(results=mixed_results()),
        )
        html = response.content.decode()
        positions = [
            html.index(f'id="search-result-{kind}-{object_id}"')
            for kind, object_id in (
                (SearchResultKind.PROMPT, 1),
                (SearchResultKind.TOOL, 2),
                (SearchResultKind.COMPARISON, 3),
                (SearchResultKind.GUIDE, 4),
                (SearchResultKind.USE_CASE, 5),
            )
        ]
        self.assertEqual(positions, sorted(positions), "results were re-ordered")

    def test_total_count_is_shown(self):
        response, _ = self.get(
            "/en/search/?q=ai%20tools",
            response=make_response(results=mixed_results()),
        )
        self.assertIn("5 results for", response.content.decode())

    def test_singular_count(self):
        response, _ = self.get(
            "/en/search/?q=ai%20tools",
            response=make_response(
                results=(make_result(SearchResultKind.TOOL, 1),)
            ),
        )
        html = response.content.decode()
        self.assertIn("1 result for", html)
        self.assertNotIn("1 results for", html)

    def test_all_five_type_counts_are_shown(self):
        response, _ = self.get(
            "/en/search/?q=ai%20tools",
            response=make_response(results=mixed_results()),
        )
        html = response.content.decode()
        for label in ("Tools", "Guides", "Prompts", "Use cases", "Comparisons"):
            with self.subTest(label=label):
                self.assertIn(f"{label}: 1", html)

    def test_zero_counts_are_shown_for_absent_types(self):
        response, _ = self.get(
            "/en/search/?q=ai%20tools",
            response=make_response(results=(make_result(SearchResultKind.TOOL, 1),)),
        )
        html = response.content.decode()
        self.assertIn("Tools: 1", html)
        for label in ("Guides", "Prompts", "Use cases", "Comparisons"):
            with self.subTest(label=label):
                self.assertIn(f"{label}: 0", html)

    def test_result_links_are_rendered(self):
        response, _ = self.get(
            "/en/search/?q=ai%20tools",
            response=make_response(results=mixed_results()),
        )
        html = response.content.decode()
        for result in mixed_results():
            with self.subTest(url=result.url):
                self.assertIn(f'href="{result.url}"', html)

    def test_no_pagination_controls(self):
        response, _ = self.get(
            "/en/search/?q=ai%20tools",
            response=make_response(results=mixed_results()),
        )
        html = response.content.decode()
        self.assertNotIn("?page=", html)
        self.assertNotIn("pagination-link", html)


class NoResultStateTests(SearchViewTestCase):
    def test_no_results_copy_names_the_query(self):
        response, _ = self.get(
            "/en/search/?q=nothing",
            response=make_response(value="nothing", results=()),
        )
        html = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("No results for", html)
        self.assertIn("nothing", html)
        self.assertIn("Try another search term or use fewer words.", html)

    def test_no_results_renders_no_result_articles(self):
        response, _ = self.get(
            "/en/search/?q=nothing",
            response=make_response(value="nothing", results=()),
        )
        self.assertNotIn("search-result-", response.content.decode())


class SearchFailureTests(SearchViewTestCase):
    def _failing(self):
        return self.get(
            "/en/search/?q=ai%20tools",
            side_effect=SearchExecutionError(
                SearchResultKind.GUIDE, "adapter raised"
            ),
        )

    def test_returns_503(self):
        response, _ = self._failing()
        self.assertEqual(response.status_code, 503)

    def test_shows_generic_copy_only(self):
        response, _ = self._failing()
        html = response.content.decode()
        self.assertIn("Search is temporarily unavailable.", html)
        self.assertIn("Please try again in a moment.", html)

    def test_leaks_no_technical_detail(self):
        response, _ = self._failing()
        html = response.content.decode()
        for fragment in ("adapter raised", "guide search failed", "SearchExecutionError",
                         "SELECT", "Traceback"):
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, html)

    def test_shows_no_results_and_no_counts(self):
        response, _ = self._failing()
        html = response.content.decode()
        self.assertNotIn("search-result-", html)
        self.assertNotIn("Results by type", html)
        self.assertNotIn("Search results", html)

    def test_keeps_the_form_and_the_query(self):
        response, _ = self.get(
            "/en/search/?q=ai%20tools",
            side_effect=SearchExecutionError(SearchResultKind.TOOL, "adapter raised"),
        )
        html = response.content.decode()
        self.assertIn('role="search"', html)
        self.assertIn('value="ai tools"', html)

    def test_uses_an_alert_role(self):
        response, _ = self._failing()
        self.assertIn('role="alert"', response.content.decode())

    def test_unexpected_exceptions_are_not_captured(self):
        with patch("search.views.search_site") as service:
            service.side_effect = RuntimeError("programming error")
            with self.assertRaises(RuntimeError):
                self.client.get("/en/search/?q=ai%20tools")
