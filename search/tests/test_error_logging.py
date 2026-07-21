"""
Beta 10.9: a failing search must be visible in the log and silent in the page.

The visitor only ever sees "temporarily unavailable", so without a log entry a
broken adapter would fail in complete silence. The entry must not become a
second leak: the search term is visitor input and the underlying SQL is
implementation detail, so neither belongs in the message.
"""
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase
from django.utils import translation

from search.exceptions import SearchExecutionError
from search.result_types import SearchResultKind

SECRET_QUERY = "supersecretneedle"


class SearchFailureLoggingTests(TestCase):
    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)

    def fail_search(self, path=f"/en/search/?q={SECRET_QUERY}", exc=None):
        error = exc or SearchExecutionError(SearchResultKind.GUIDE, "adapter raised")
        with patch("search.views.search_site") as service:
            service.side_effect = error
            with self.assertLogs("search.views", level="ERROR") as captured:
                response = self.client.get(path)
        return response, captured

    def test_a_failing_search_is_logged_as_an_error(self):
        response, captured = self.fail_search()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(len(captured.records), 1)
        self.assertEqual(captured.records[0].levelname, "ERROR")

    def test_the_message_names_the_adapter_and_the_language(self):
        _, captured = self.fail_search()
        message = captured.records[0].getMessage()
        self.assertIn(str(SearchResultKind.GUIDE), message)
        self.assertIn("adapter raised", message)
        self.assertIn("en", message)

    def test_the_message_does_not_contain_the_search_term(self):
        _, captured = self.fail_search()
        self.assertNotIn(SECRET_QUERY, captured.records[0].getMessage())

    def test_the_message_does_not_contain_sql(self):
        # The adapters build real SQL; a database error carrying it must not
        # be pasted into the message just because it caused the failure.
        cause = RuntimeError('SELECT "guides_guide"."id" FROM "guides_guide" WHERE ...')
        error = SearchExecutionError(SearchResultKind.GUIDE, "adapter raised")
        error.__cause__ = cause
        _, captured = self.fail_search(exc=error)
        message = captured.records[0].getMessage()
        for fragment in ("SELECT", "FROM", "WHERE", "guides_guide"):
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, message)

    def test_the_traceback_is_kept_for_diagnosis(self):
        # exc_info is what makes the entry actionable, and it never reaches
        # the visitor.
        _, captured = self.fail_search()
        self.assertIsNotNone(captured.records[0].exc_info)

    def test_a_successful_search_logs_nothing(self):
        from search.query import SearchQueryIssue
        from search.tests.search_page_fixtures import empty_query_response

        with patch("search.views.search_site") as service:
            service.return_value = empty_query_response(SearchQueryIssue.EMPTY)
            with self.assertNoLogs("search.views", level="ERROR"):
                response = self.client.get("/en/search/?q=ai")
        self.assertEqual(response.status_code, 200)

    def test_the_page_itself_reveals_no_diagnostics(self):
        response, _ = self.fail_search()
        html = response.content.decode()
        for leak in ("adapter raised", "Traceback", "SELECT", "SearchExecutionError"):
            with self.subTest(leak=leak):
                self.assertNotIn(leak, html)
