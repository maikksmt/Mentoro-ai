"""
Beta 10.6: the contract every search adapter fulfils, checked across all five.

The editorial adapters share fixtures and snapshot semantics and are covered in
test_editorial_adapters.py; tools have neither. What all five do share is this
contract, so it is verified here in one place - including the tool adapter,
which the editorial fixtures cannot build.
"""
from unittest import skipUnless
from unittest.mock import patch

from django.db import connection
from django.test import SimpleTestCase, TestCase

from search.adapters.comparisons import ComparisonSearchAdapter
from search.adapters.guides import GuideSearchAdapter
from search.adapters.prompts import PromptSearchAdapter
from search.adapters.tools import ToolSearchAdapter
from search.adapters.usecases import UseCaseSearchAdapter
from search.fts import SearchBackendUnavailable, UnsupportedSearchLanguage
from search.query import NormalizedSearchQuery, SearchQueryIssue, normalize_search_query
from search.result_types import SearchResultKind

ADAPTER_CLASSES = (
    GuideSearchAdapter,
    PromptSearchAdapter,
    UseCaseSearchAdapter,
    ComparisonSearchAdapter,
    ToolSearchAdapter,
)

postgresql_only = skipUnless(
    connection.vendor == "postgresql", "PostgreSQL full-text search required"
)


class AdapterDeclarationTests(SimpleTestCase):
    def test_every_content_kind_has_an_adapter(self):
        self.assertEqual(
            {adapter_class.kind for adapter_class in ADAPTER_CLASSES},
            set(SearchResultKind),
        )

    def test_kinds_are_distinct(self):
        kinds = [adapter_class.kind for adapter_class in ADAPTER_CLASSES]
        self.assertEqual(len(kinds), len(set(kinds)))

    def test_kind_is_declared_on_the_class(self):
        for adapter_class in ADAPTER_CLASSES:
            with self.subTest(adapter=adapter_class.__name__):
                self.assertIsInstance(adapter_class.kind, SearchResultKind)


class AdapterSignatureTests(SimpleTestCase):
    def test_search_takes_query_and_language_keyword_only(self):
        import inspect

        for adapter_class in ADAPTER_CLASSES:
            with self.subTest(adapter=adapter_class.__name__):
                signature = inspect.signature(adapter_class.search)
                parameters = list(signature.parameters.values())[1:]
                self.assertEqual(
                    [p.name for p in parameters], ["query", "language_code"]
                )
                for parameter in parameters:
                    self.assertIs(parameter.kind, inspect.Parameter.KEYWORD_ONLY)

    def test_positional_arguments_are_rejected(self):
        for adapter_class in ADAPTER_CLASSES:
            with self.subTest(adapter=adapter_class.__name__):
                with self.assertRaises(TypeError):
                    adapter_class().search(normalize_search_query("machine"), "en")


@postgresql_only
class AdapterFailClosedTests(TestCase):
    def test_unsearchable_query_is_rejected_before_any_database_access(self):
        for adapter_class in ADAPTER_CLASSES:
            for issue in SearchQueryIssue:
                with self.subTest(adapter=adapter_class.__name__, issue=issue):
                    query = NormalizedSearchQuery(value="a", issue=issue)
                    with self.assertNumQueries(0):
                        with self.assertRaises(ValueError):
                            adapter_class().search(query=query, language_code="en")

    def test_unsupported_language_is_rejected_before_any_database_access(self):
        for adapter_class in ADAPTER_CLASSES:
            for language_code in ("fr", "", "EN", "de-at"):
                with self.subTest(
                    adapter=adapter_class.__name__, language=language_code
                ):
                    with self.assertNumQueries(0):
                        with self.assertRaises(UnsupportedSearchLanguage):
                            adapter_class().search(
                                query=normalize_search_query("machine"),
                                language_code=language_code,
                            )

    def test_non_postgresql_backend_fails_closed(self):
        for adapter_class in ADAPTER_CLASSES:
            with self.subTest(adapter=adapter_class.__name__):
                with patch("search.fts.connection") as fake_connection:
                    fake_connection.vendor = "sqlite"
                    with self.assertRaises(SearchBackendUnavailable):
                        adapter_class().search(
                            query=normalize_search_query("machine"),
                            language_code="en",
                        )

    def test_no_adapter_swallows_a_failure_into_an_empty_result(self):
        # A failed search and an empty result must never look the same.
        for adapter_class in ADAPTER_CLASSES:
            with self.subTest(adapter=adapter_class.__name__):
                with patch("search.fts.connection") as fake_connection:
                    fake_connection.vendor = "sqlite"
                    try:
                        result = adapter_class().search(
                            query=normalize_search_query("machine"),
                            language_code="en",
                        )
                    except SearchBackendUnavailable:
                        continue
                    self.fail(f"returned {result!r} instead of raising")


@postgresql_only
class AdapterResultShapeTests(TestCase):
    """Shape guarantees that hold even with no content in the database."""

    def test_empty_database_yields_an_empty_tuple(self):
        for adapter_class in ADAPTER_CLASSES:
            for language_code in ("en", "de"):
                with self.subTest(
                    adapter=adapter_class.__name__, language=language_code
                ):
                    results = adapter_class().search(
                        query=normalize_search_query("nothingmatchesthistoken"),
                        language_code=language_code,
                    )
                    self.assertIsInstance(results, tuple)
                    self.assertEqual(results, ())
