from dataclasses import FrozenInstanceError

from django.test import SimpleTestCase

from search.query import (
    MAX_SEARCH_QUERY_LENGTH,
    MIN_SEARCH_QUERY_LENGTH,
    NormalizedSearchQuery,
    SearchQueryIssue,
    normalize_search_query,
    normalize_text,
)


class NormalizeSearchQueryEmptinessTests(SimpleTestCase):
    def test_none_is_empty(self):
        result = normalize_search_query(None)
        self.assertEqual(result.value, "")
        self.assertIs(result.issue, SearchQueryIssue.EMPTY)
        self.assertFalse(result.is_searchable)

    def test_empty_string_is_empty(self):
        self.assertIs(normalize_search_query("").issue, SearchQueryIssue.EMPTY)

    def test_whitespace_only_is_empty(self):
        for raw in ("   ", "\t", "\n", " \t\n\r ", " "):
            with self.subTest(raw=raw):
                result = normalize_search_query(raw)
                self.assertEqual(result.value, "")
                self.assertIs(result.issue, SearchQueryIssue.EMPTY)

    def test_control_characters_only_is_empty(self):
        self.assertIs(normalize_search_query("\x00\x1b\x07").issue, SearchQueryIssue.EMPTY)


class NormalizeSearchQueryLengthTests(SimpleTestCase):
    def test_single_character_is_too_short(self):
        result = normalize_search_query("a")
        self.assertEqual(result.value, "a")
        self.assertIs(result.issue, SearchQueryIssue.TOO_SHORT)
        self.assertFalse(result.is_searchable)

    def test_minimum_length_is_searchable(self):
        result = normalize_search_query("ai")
        self.assertEqual(result.value, "ai")
        self.assertIsNone(result.issue)
        self.assertTrue(result.is_searchable)

    def test_exactly_max_length_is_searchable(self):
        raw = "a" * MAX_SEARCH_QUERY_LENGTH
        result = normalize_search_query(raw)
        self.assertEqual(len(result.value), MAX_SEARCH_QUERY_LENGTH)
        self.assertIsNone(result.issue)

    def test_one_over_max_length_is_too_long(self):
        raw = "a" * (MAX_SEARCH_QUERY_LENGTH + 1)
        result = normalize_search_query(raw)
        self.assertIs(result.issue, SearchQueryIssue.TOO_LONG)
        self.assertFalse(result.is_searchable)

    def test_too_long_query_is_not_silently_truncated(self):
        raw = "b" * (MAX_SEARCH_QUERY_LENGTH + 25)
        result = normalize_search_query(raw)
        self.assertEqual(len(result.value), MAX_SEARCH_QUERY_LENGTH + 25)

    def test_length_is_measured_after_normalization(self):
        # Trailing whitespace and control characters must not push a query
        # that is exactly at the limit over it.
        raw = "a" * MAX_SEARCH_QUERY_LENGTH + "   \x00\n"
        self.assertIsNone(normalize_search_query(raw).issue)

    def test_constants_are_the_documented_bounds(self):
        self.assertEqual(MIN_SEARCH_QUERY_LENGTH, 2)
        self.assertEqual(MAX_SEARCH_QUERY_LENGTH, 100)


class NormalizeSearchQueryWhitespaceTests(SimpleTestCase):
    def test_surrounding_whitespace_is_removed(self):
        self.assertEqual(normalize_search_query("  ai tools  ").value, "ai tools")

    def test_inner_whitespace_is_collapsed(self):
        self.assertEqual(normalize_search_query("ai     tools").value, "ai tools")

    def test_tabs_and_newlines_become_single_spaces(self):
        self.assertEqual(normalize_search_query("ai\t\ntools").value, "ai tools")

    def test_newline_separated_words_do_not_merge(self):
        # Newlines are Cc control characters; dropping them outright would
        # silently produce "aitools".
        self.assertEqual(normalize_search_query("ai\ntools").value, "ai tools")


class NormalizeSearchQueryUnicodeTests(SimpleTestCase):
    def test_nfkc_ligature_is_decomposed(self):
        self.assertEqual(normalize_search_query("ﬁnd").value, "find")

    def test_nfkc_fullwidth_is_folded(self):
        self.assertEqual(normalize_search_query("ＡＢ").value, "AB")

    def test_nfkc_composes_combining_diaeresis(self):
        composed = normalize_search_query("Übersetzung").value
        self.assertEqual(composed, "Übersetzung")

    def test_umlauts_are_preserved_not_transliterated(self):
        self.assertEqual(normalize_search_query("Übersetzung").value, "Übersetzung")

    def test_accents_are_preserved(self):
        self.assertEqual(normalize_search_query("café").value, "café")

    def test_emoji_is_preserved(self):
        self.assertEqual(normalize_search_query("🔥 tools").value, "🔥 tools")

    def test_non_whitespace_control_characters_are_removed(self):
        self.assertEqual(normalize_search_query("ai\x00\x1btools").value, "aitools")


class NormalizeSearchQueryPreservationTests(SimpleTestCase):
    def test_original_casing_is_preserved(self):
        self.assertEqual(normalize_search_query("ChatGPT").value, "ChatGPT")

    def test_punctuation_is_preserved(self):
        self.assertEqual(normalize_search_query("what is ai?").value, "what is ai?")

    def test_hyphen_is_preserved(self):
        self.assertEqual(normalize_search_query("real-time").value, "real-time")

    def test_search_operators_are_not_parsed(self):
        # No query syntax of our own: these stay literal characters.
        for raw in ('"exact phrase"', "-excluded", "a AND b", "a | b"):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_search_query(raw).value, raw)


class NormalizedSearchQueryTypeTests(SimpleTestCase):
    def test_is_immutable(self):
        query = normalize_search_query("ai tools")
        with self.assertRaises(FrozenInstanceError):
            query.value = "changed"

    def test_uses_slots(self):
        self.assertFalse(hasattr(normalize_search_query("ai tools"), "__dict__"))

    def test_is_searchable_only_when_issue_is_none(self):
        self.assertTrue(NormalizedSearchQuery(value="ai", issue=None).is_searchable)
        for issue in SearchQueryIssue:
            with self.subTest(issue=issue):
                self.assertFalse(
                    NormalizedSearchQuery(value="ai", issue=issue).is_searchable
                )

    def test_issue_values_are_stable_strings(self):
        self.assertEqual(SearchQueryIssue.EMPTY, "empty")
        self.assertEqual(SearchQueryIssue.TOO_SHORT, "too_short")
        self.assertEqual(SearchQueryIssue.TOO_LONG, "too_long")


class NormalizeTextTests(SimpleTestCase):
    def test_is_idempotent(self):
        once = normalize_text("  Ai\t\ntools\x00  ")
        self.assertEqual(normalize_text(once), once)

    def test_does_not_change_case(self):
        self.assertEqual(normalize_text("ChatGPT"), "ChatGPT")
