from unittest.mock import patch

from django.test import SimpleTestCase

from search.fts import (
    POSTGRES_SEARCH_CONFIG_BY_LANGUAGE,
    SEARCH_QUERY_TYPE,
    SEARCH_RANK_NORMALIZATION,
    SearchBackendUnavailable,
    UnsupportedSearchLanguage,
    build_search_query,
    require_postgresql,
    resolve_search_config,
)
from search.query import NormalizedSearchQuery, SearchQueryIssue, normalize_search_query


class SearchConfigTests(SimpleTestCase):
    def test_project_languages_map_to_postgresql_configurations(self):
        self.assertEqual(resolve_search_config("en"), "english")
        self.assertEqual(resolve_search_config("de"), "german")

    def test_unknown_language_fails_closed(self):
        for language_code in ("fr", "", "EN", "en-gb", "xx"):
            with self.subTest(language_code=language_code), self.assertRaises(UnsupportedSearchLanguage):
                resolve_search_config(language_code)

    def test_unknown_language_never_falls_back_to_english(self):
        # A silent fallback would stem German text with English rules.
        with self.assertRaisesMessage(UnsupportedSearchLanguage, "no search configuration"):
            resolve_search_config("de-at")

    def test_unsupported_language_is_a_value_error(self):
        self.assertTrue(issubclass(UnsupportedSearchLanguage, ValueError))

    def test_configuration_map_covers_the_project_languages(self):
        from django.conf import settings

        self.assertEqual(
            set(POSTGRES_SEARCH_CONFIG_BY_LANGUAGE),
            {code for code, _label in settings.LANGUAGES},
        )


class RankNormalizationTests(SimpleTestCase):
    def test_normalization_is_the_documented_bounded_option(self):
        # Bit 32 maps a raw rank into [0, 1) so ranks stay comparable across
        # content types without any per-adapter rescaling.
        self.assertEqual(SEARCH_RANK_NORMALIZATION, 32)

    def test_query_type_is_websearch(self):
        self.assertEqual(SEARCH_QUERY_TYPE, "websearch")


class BuildSearchQueryTests(SimpleTestCase):
    def test_builds_a_query_for_a_searchable_query(self):
        search_query = build_search_query(
            normalize_search_query("machine learning"), config="english"
        )
        self.assertIsNotNone(search_query)

    def test_unsearchable_query_fails_closed(self):
        for issue in SearchQueryIssue:
            with self.subTest(issue=issue):
                query = NormalizedSearchQuery(value="a", issue=issue)
                with self.assertRaisesMessage(ValueError, "unsearchable query"):
                    build_search_query(query, config="english")


class BackendGuardTests(SimpleTestCase):
    def test_postgresql_is_accepted(self):
        with patch("search.fts.connection") as fake_connection:
            fake_connection.vendor = "postgresql"
            require_postgresql()

    def test_other_backends_fail_closed(self):
        for vendor in ("sqlite", "mysql", "oracle"):
            with self.subTest(vendor=vendor), patch("search.fts.connection") as fake_connection:
                fake_connection.vendor = vendor
                with self.assertRaises(SearchBackendUnavailable):
                    require_postgresql()

    def test_error_names_the_actual_backend(self):
        with patch("search.fts.connection") as fake_connection:
            fake_connection.vendor = "sqlite"
            with self.assertRaisesMessage(SearchBackendUnavailable, "'sqlite'"):
                require_postgresql()
