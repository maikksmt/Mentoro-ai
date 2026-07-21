"""
Beta 10.9: the central card-summary rule.

Every editorial card shortens its intro through core.text, so the rules live
here once: visible text only, cut on a word boundary, exactly "..." when
something was dropped and nothing when it was not.

Deliberately SimpleTestCase - none of this touches the database.
"""
from django.template import Context, Template
from django.test import SimpleTestCase
from django.utils.safestring import SafeString

from core.text import (
    EDITORIAL_INTRO_MAX_CHARS,
    summarize_html,
    truncate_at_word_boundary,
    visible_text,
)

LONG = "word " * 200


class VisibleTextTests(SimpleTestCase):
    def test_tags_are_removed(self):
        self.assertEqual(visible_text("<p>Hello <strong>world</strong></p>"), "Hello world")

    def test_block_boundaries_do_not_glue_words_together(self):
        self.assertEqual(
            visible_text("<p>First sentence.</p><p>Second sentence.</p>"),
            "First sentence. Second sentence.",
        )

    def test_line_breaks_do_not_glue_words_together(self):
        for markup in (
            "Line one<br>Line two",
            "Line one<br/>Line two",
            "Line one<BR />Line two",
            # Real stored content, not a hypothetical: guide intros carry
            # editor-emitted attributes on the break tag, and matching only
            # the bare form rendered "assistant.Learn" on the guide list.
            'Line one<br data-start="749" data-end="752">Line two',
            'Line one<br class="x" />Line two',
        ):
            with self.subTest(markup=markup):
                self.assertEqual(visible_text(markup), "Line one Line two")

    def test_a_tag_merely_starting_with_br_is_not_treated_as_a_break(self):
        self.assertEqual(visible_text("<blockquote>Quoted</blockquote>"), "Quoted")

    def test_list_items_are_separated(self):
        self.assertEqual(visible_text("<ul><li>Alpha</li><li>Beta</li></ul>"), "Alpha Beta")

    def test_entities_are_decoded_exactly_once(self):
        self.assertEqual(visible_text("<p>AI &amp; automation</p>"), "AI & automation")

    def test_whitespace_is_collapsed(self):
        self.assertEqual(visible_text("<p>a\n\n   b\t c</p>"), "a b c")

    def test_empty_values_are_empty_strings(self):
        for value in ("", None, "<p></p>"):
            with self.subTest(value=value):
                self.assertEqual(visible_text(value), "")

    def test_result_is_never_a_safestring(self):
        # A SafeString would let a caller pipe this through |safe and get
        # markup back into the page.
        for value in ("<p>plain</p>", SafeString("<p>already safe</p>"), LONG):
            with self.subTest(value=value[:20]):
                self.assertNotIsInstance(visible_text(value), SafeString)

    def test_markup_in_the_source_becomes_literal_text(self):
        self.assertEqual(visible_text("<p>a &lt;b&gt; c</p>"), "a <b> c")


class TruncateAtWordBoundaryTests(SimpleTestCase):
    def test_short_text_is_returned_untouched_without_dots(self):
        self.assertEqual(truncate_at_word_boundary("Short intro.", 100), "Short intro.")

    def test_text_of_exactly_the_limit_keeps_no_dots(self):
        text = "a" * 40
        self.assertEqual(truncate_at_word_boundary(text, 40), text)

    def test_longer_text_gets_exactly_three_periods(self):
        result = truncate_at_word_boundary("alpha beta gamma delta", 12)
        self.assertTrue(result.endswith("..."))
        self.assertFalse(result.endswith("...."))
        self.assertNotIn("…", result)

    def test_no_word_is_split(self):
        result = truncate_at_word_boundary("alpha beta gamma delta", 12)
        self.assertEqual(result, "alpha beta...")

    def test_every_word_in_the_result_is_a_whole_word_of_the_source(self):
        source = "Prompting techniques for everyday office work with assistants"
        for limit in range(5, len(source)):
            with self.subTest(limit=limit):
                result = truncate_at_word_boundary(source, limit)
                body = result[:-3] if result.endswith("...") else result
                for word in body.split():
                    self.assertIn(word, source.split())

    def test_the_result_stays_within_the_limit_whenever_a_boundary_allows_it(self):
        source = "Prompting techniques for everyday office work with assistants"
        first_word = len(source.split()[0])
        for limit in range(first_word, len(source)):
            with self.subTest(limit=limit):
                result = truncate_at_word_boundary(source, limit)
                body = result[:-3] if result.endswith("...") else result
                self.assertLessEqual(len(body), limit)

    def test_dangling_punctuation_is_dropped_before_the_marker(self):
        self.assertEqual(truncate_at_word_boundary("alpha beta, gamma", 12), "alpha beta...")

    def test_a_first_word_longer_than_the_limit_is_kept_whole(self):
        # No boundary exists inside the window, and splitting the word is the
        # one thing this function must never do - so the limit yields.
        self.assertEqual(truncate_at_word_boundary("x" * 50 + " tail", 10), "x" * 50 + "...")

    def test_a_single_unbroken_token_is_returned_untouched(self):
        # Nothing was dropped, so it gets no marker either.
        self.assertEqual(truncate_at_word_boundary("x" * 50, 10), "x" * 50)


class SummarizeHtmlTests(SimpleTestCase):
    def test_html_in_plain_summary_out(self):
        self.assertEqual(
            summarize_html("<p>Alpha <em>beta</em> gamma delta</p>", 12),
            "Alpha beta...",
        )

    def test_the_limit_applies_to_visible_text_not_to_markup(self):
        # 200 characters of tags around 11 characters of text must not count
        # as a long intro.
        markup = '<p class="' + "x" * 300 + '">Alpha beta.</p>'
        self.assertEqual(summarize_html(markup), "Alpha beta.")

    def test_default_limit_is_the_shared_constant(self):
        result = summarize_html("<p>" + LONG + "</p>")
        self.assertLessEqual(len(result) - 3, EDITORIAL_INTRO_MAX_CHARS)
        self.assertTrue(result.endswith("..."))

    def test_result_is_never_a_safestring(self):
        self.assertNotIsInstance(summarize_html("<p>" + LONG + "</p>"), SafeString)


class SummarizeFilterTests(SimpleTestCase):
    def render(self, template, **context):
        return Template("{% load text_extras %}" + template).render(Context(context))

    def test_filter_shortens_and_escapes(self):
        out = self.render("{{ value|summarize }}", value="<p>AI &amp; automation</p>")
        # Decoded once by the summary, escaped once by the template: the
        # visitor sees "AI & automation", not "AI &amp; automation".
        self.assertEqual(out, "AI &amp; automation")

    def test_filter_accepts_a_custom_limit(self):
        self.assertEqual(
            self.render("{{ value|summarize:12 }}", value="<p>alpha beta gamma</p>"),
            "alpha beta...",
        )

    def test_script_markup_cannot_survive_the_filter(self):
        out = self.render(
            "{{ value|summarize }}", value='<script>alert(1)</script><p>Intro</p>'
        )
        self.assertNotIn("<script>", out)
        self.assertNotIn("<p>", out)
