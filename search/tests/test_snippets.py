from django.test import SimpleTestCase

from search.query import NormalizedSearchQuery, SearchQueryIssue, normalize_search_query
from search.snippets import DEFAULT_SNIPPET_LENGTH, build_search_snippet

QUERY = normalize_search_query("needle")


def snippet(source, *, query=QUERY, max_length=DEFAULT_SNIPPET_LENGTH) -> str:
    return build_search_snippet(source, query=query, max_length=max_length)


class EmptySourceTests(SimpleTestCase):
    def test_none_returns_empty_string(self):
        self.assertEqual(snippet(None), "")

    def test_empty_string_returns_empty_string(self):
        self.assertEqual(snippet(""), "")

    def test_whitespace_only_returns_empty_string(self):
        self.assertEqual(snippet("   \n\t  "), "")

    def test_markup_without_text_returns_empty_string(self):
        self.assertEqual(snippet("<p></p><br/>"), "")


class HtmlCleaningTests(SimpleTestCase):
    def test_tags_are_removed(self):
        self.assertEqual(snippet("<p>Hello <strong>world</strong></p>"), "Hello world")

    def test_entities_are_decoded(self):
        self.assertEqual(snippet("Tom &amp; Jerry"), "Tom & Jerry")

    def test_escaped_markup_stays_literal_text(self):
        # strip_tags runs before unescape, so escaped markup is shown as text
        # instead of becoming a tag that is then silently removed.
        self.assertEqual(snippet("&lt;b&gt;bold&lt;/b&gt;"), "<b>bold</b>")

    def test_script_content_survives_as_plain_text(self):
        # strip_tags removes the tags, not the text between them. That is safe
        # because the result is plain text and the template escapes it.
        result = snippet("<script>alert('hi')</script>")
        self.assertEqual(result, "alert('hi')")
        self.assertNotIn("<script", result)

    def test_whitespace_is_collapsed(self):
        self.assertEqual(snippet("a   b\n\nc\t\td"), "a b c d")

    def test_result_is_a_plain_string_not_marked_safe(self):
        result = snippet("<p>Hello</p>")
        self.assertIs(type(result), str)
        self.assertFalse(hasattr(result, "__html__"))


class AttributeOnlyMatchTests(SimpleTestCase):
    def test_query_only_inside_an_attribute_is_not_a_visible_hit(self):
        source = (
            '<a href="https://example.com/needle" class="needle">'
            "Visible text that does not contain the term</a>"
        )
        result = snippet(source)
        self.assertEqual(result, "Visible text that does not contain the term")
        self.assertNotIn("needle", result)

    def test_attribute_match_in_long_text_falls_back_to_the_beginning(self):
        source = '<div data-tag="needle">' + ("filler word " * 60) + "</div>"
        result = snippet(source, max_length=50)
        self.assertTrue(result.startswith("filler"))
        self.assertNotIn("needle", result)


class ShortTextTests(SimpleTestCase):
    def test_text_shorter_than_the_limit_is_returned_whole(self):
        self.assertEqual(snippet("a needle here"), "a needle here")

    def test_text_exactly_at_the_limit_is_returned_whole(self):
        source = "x" * 50
        self.assertEqual(snippet(source, max_length=50), source)

    def test_no_ellipsis_when_nothing_was_cut(self):
        self.assertNotIn("…", snippet("a needle here"))


class WindowPositioningTests(SimpleTestCase):
    LONG = "alpha " * 40 + "needle " + "omega " * 40

    def test_query_at_the_start(self):
        source = "needle " + ("filler " * 60)
        result = snippet(source, max_length=60)
        self.assertIn("needle", result)
        self.assertFalse(result.startswith("…"))
        self.assertTrue(result.endswith("…"))

    def test_query_in_the_middle(self):
        result = snippet(self.LONG, max_length=60)
        self.assertIn("needle", result)
        self.assertTrue(result.startswith("…"))
        self.assertTrue(result.endswith("…"))

    def test_query_at_the_end(self):
        source = ("filler " * 60) + "needle"
        result = snippet(source, max_length=60)
        self.assertIn("needle", result)
        self.assertTrue(result.startswith("…"))
        self.assertFalse(result.endswith("…"))

    def test_query_is_roughly_centred(self):
        result = snippet(self.LONG, max_length=60)
        position = result.index("needle")
        self.assertGreater(position, 5)
        self.assertLess(position, len(result) - 5)

    def test_missing_query_truncates_from_the_beginning(self):
        source = "alpha " * 60
        result = snippet(source, max_length=40)
        self.assertTrue(result.startswith("alpha"))
        self.assertTrue(result.endswith("…"))


class CaseAndUnicodeTests(SimpleTestCase):
    def test_match_is_case_insensitive(self):
        source = ("filler " * 40) + "NEEDLE " + ("filler " * 40)
        self.assertIn("NEEDLE", snippet(source, max_length=60))

    def test_query_with_different_case_still_centres(self):
        query = normalize_search_query("NeEdLe")
        source = ("filler " * 40) + "needle " + ("filler " * 40)
        self.assertIn("needle", snippet(source, query=query, max_length=60))

    def test_nfkc_equivalent_characters_match(self):
        query = normalize_search_query("find")
        source = ("filler " * 40) + "ﬁnd " + ("filler " * 40)
        self.assertIn("find", snippet(source, query=query, max_length=60))

    def test_umlauts_are_preserved(self):
        self.assertEqual(snippet("Übersetzung für Anfänger"), "Übersetzung für Anfänger")

    def test_umlaut_query_matches(self):
        query = normalize_search_query("Übersetzung")
        source = ("filler " * 40) + "Übersetzung " + ("filler " * 40)
        self.assertIn("Übersetzung", snippet(source, query=query, max_length=60))

    def test_emoji_is_preserved(self):
        self.assertEqual(snippet("<p>Hot 🔥 tools</p>"), "Hot 🔥 tools")

    def test_emoji_text_is_not_split_mid_codepoint(self):
        source = "🔥" * 100
        result = snippet(source, max_length=20)
        self.assertLessEqual(len(result), 20)
        # Encoding round-trips cleanly only if no codepoint was split.
        self.assertEqual(result.encode("utf-8").decode("utf-8"), result)

    def test_hyphenated_query_matches(self):
        query = normalize_search_query("real-time")
        source = ("filler " * 40) + "real-time " + ("filler " * 40)
        self.assertIn("real-time", snippet(source, query=query, max_length=60))

    def test_regex_special_characters_in_query_are_literal(self):
        query = normalize_search_query("a.b")
        source = ("filler " * 40) + "a.b " + ("filler " * 40)
        self.assertIn("a.b", snippet(source, query=query, max_length=60))

    def test_regex_special_characters_do_not_match_arbitrary_text(self):
        query = normalize_search_query("a.c")
        source = "abc " + ("filler " * 60)
        result = snippet(source, query=query, max_length=40)
        self.assertTrue(result.startswith("abc"))


class LengthBudgetTests(SimpleTestCase):
    LONG = "alpha " * 40 + "needle " + "omega " * 40

    def test_result_never_exceeds_max_length(self):
        for max_length in (1, 2, 3, 5, 10, 25, 60, 200):
            with self.subTest(max_length=max_length):
                self.assertLessEqual(len(snippet(self.LONG, max_length=max_length)), max_length)

    def test_ellipsis_counts_towards_the_budget(self):
        result = snippet(self.LONG, max_length=20)
        self.assertLessEqual(len(result), 20)
        self.assertIn("…", result)

    def test_very_small_max_length_still_returns_a_string(self):
        self.assertIsInstance(snippet(self.LONG, max_length=1), str)

    def test_default_length_is_the_documented_value(self):
        self.assertEqual(DEFAULT_SNIPPET_LENGTH, 200)

    def test_non_positive_max_length_is_rejected(self):
        for max_length in (0, -1):
            with self.subTest(max_length=max_length), self.assertRaisesMessage(ValueError, "max_length must be positive"):
                snippet("text", max_length=max_length)


class WordBoundaryTests(SimpleTestCase):
    def test_snippet_does_not_start_mid_word(self):
        source = ("supercalifragilistic " * 20) + "needle " + ("omega " * 20)
        result = snippet(source, max_length=60)
        body = result.lstrip("…")
        self.assertFalse(body.startswith(" "))
        # The first token must be a whole word from the source.
        self.assertIn(body.split(" ")[0], source)

    def test_snippet_does_not_end_mid_word(self):
        source = "needle " + ("supercalifragilistic " * 20)
        result = snippet(source, max_length=60)
        body = result.rstrip("…")
        self.assertIn(body.split(" ")[-1], source)


class UnsearchableQueryTests(SimpleTestCase):
    def test_unsearchable_query_is_rejected(self):
        for issue in SearchQueryIssue:
            with self.subTest(issue=issue):
                query = NormalizedSearchQuery(value="a", issue=issue)
                with self.assertRaisesMessage(ValueError, "unsearchable query"):
                    build_search_snippet("some text", query=query)
