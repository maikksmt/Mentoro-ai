"""
Beta 10.8: markup, accessibility and escaping of the search page.

Asserts on semantics and content rather than Tailwind class sequences, so
restyling does not break these tests while a missing label or an unescaped
query still does.
"""
import re
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase
from django.utils import translation

from search.result_types import SearchResultKind
from search.tests.search_page_fixtures import make_response, make_result, mixed_results


class SearchTemplateTestCase(TestCase):
    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)

    def html(self, url="/en/search/?q=ai%20tools", response=None):
        with patch("search.views.search_site") as service:
            service.return_value = response or make_response(results=mixed_results())
            return self.client.get(url).content.decode()

    def search_form(self, html):
        """
        Isolates the page's own search form.

        base.html carries its own forms - the language switcher in particular,
        with a CSRF token and a hidden field - so asserting against the whole
        document would test the site chrome instead of this page.
        """
        match = re.search(r'<form[^>]*role="search".*?</form>', html, re.DOTALL)
        self.assertIsNotNone(match, "no search form found")
        return match.group(0)


class FormStructureTests(SearchTemplateTestCase):
    def test_form_is_a_get_search_landmark(self):
        form = self.search_form(self.html())
        self.assertIn('role="search"', form)
        self.assertIn('method="get"', form)

    def test_form_action_stays_in_the_request_language(self):
        self.assertIn('action="/de/search/"', self.html("/de/search/?q=KI"))
        self.assertIn('action="/en/search/"', self.html("/en/search/?q=AI"))

    def test_input_has_a_real_associated_label(self):
        html = self.html()
        label = re.search(r'<label[^>]*for="search-input"[^>]*>', html)
        self.assertIsNotNone(label, "search input has no associated label")
        self.assertIn('id="search-input"', html)

    def test_input_is_a_search_field_named_q(self):
        html = self.html()
        field = re.search(r'<input[^>]*id="search-input"[^>]*>', html).group(0)
        self.assertIn('type="search"', field)
        self.assertIn('name="q"', field)
        self.assertIn('maxlength="100"', field)

    def test_submit_is_a_real_button(self):
        self.assertRegex(self.html(), r'<button[^>]*type="submit"[^>]*>')

    def test_no_csrf_token_on_a_get_form(self):
        self.assertNotIn("csrfmiddlewaretoken", self.search_form(self.html()))

    def test_no_hidden_filter_fields(self):
        form = self.search_form(self.html())
        self.assertNotIn('type="hidden"', form)
        for name in ("type", "kind", "filter", "sort", "page", "limit", "offset"):
            with self.subTest(name=name):
                self.assertNotIn(f'name="{name}"', form)

    def test_no_autofocus_once_results_are_shown(self):
        self.assertNotIn("autofocus", self.search_form(self.html()))

    def test_autofocus_on_the_initial_state(self):
        from search.query import SearchQueryIssue
        from search.tests.search_page_fixtures import empty_query_response

        form = self.search_form(
            self.html("/en/search/", response=empty_query_response(SearchQueryIssue.EMPTY))
        )
        self.assertIn("autofocus", form)


class HeadingStructureTests(SearchTemplateTestCase):
    def test_exactly_one_h1(self):
        self.assertEqual(len(re.findall(r"<h1[\s>]", self.html())), 1)

    def test_results_section_is_labelled_by_an_h2(self):
        html = self.html()
        self.assertIn('aria-labelledby="search-results-heading"', html)
        self.assertRegex(html, r'<h2[^>]*id="search-results-heading"')

    def test_each_result_has_a_unique_heading_id(self):
        html = self.html()
        ids = re.findall(r'id="(search-result-[^"]+)"', html)
        self.assertEqual(len(ids), 5)
        self.assertEqual(len(ids), len(set(ids)))

    def test_each_result_article_is_labelled_by_its_heading(self):
        html = self.html()
        for kind, object_id in (
            (SearchResultKind.PROMPT, 1),
            (SearchResultKind.TOOL, 2),
        ):
            with self.subTest(kind=kind):
                self.assertIn(
                    f'aria-labelledby="search-result-{kind}-{object_id}"', html
                )

    def test_results_use_an_ordered_list_of_articles(self):
        html = self.html()
        self.assertIn("<ol", html)
        self.assertEqual(len(re.findall(r"<article[\s>]", html)), 5)


class BadgeAndCountTests(SearchTemplateTestCase):
    def test_every_badge_carries_visible_text(self):
        html = self.html()
        for label in ("Tool", "Guide", "Prompt", "Use case", "Comparison"):
            with self.subTest(label=label):
                self.assertRegex(html, rf'<span class="badge badge-\w+">\s*{label}\s*</span>')

    def test_badges_reuse_the_existing_project_classes(self):
        html = self.html()
        for badge_class in (
            "badge-tool",
            "badge-guide",
            "badge-prompt",
            "badge-usecase",
            "badge-compare",
        ):
            with self.subTest(badge_class=badge_class):
                self.assertIn(badge_class, html)

    def test_counts_are_not_interactive(self):
        html = self.html()
        counts_block = html[html.index("Results by type"):html.index("<ol")]
        self.assertNotIn("<button", counts_block)
        self.assertNotIn("<a ", counts_block)

    def test_counts_are_a_list(self):
        html = self.html()
        self.assertIn("Results by type", html)
        self.assertIn("<ul", html)


class EscapingTests(SearchTemplateTestCase):
    XSS = '<script>alert(1)</script>'
    ATTR_XSS = '"><img src=x onerror=alert(1)>'

    def test_query_value_is_escaped_in_the_form(self):
        html = self.html(
            "/en/search/?q=x", response=make_response(value=self.XSS, results=())
        )
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_attribute_breaking_query_is_escaped(self):
        html = self.html(
            "/en/search/?q=x", response=make_response(value=self.ATTR_XSS, results=())
        )
        self.assertNotIn("<img src=x onerror=alert(1)>", html)
        self.assertIn("&lt;img", html)

    def test_result_title_is_escaped(self):
        html = self.html(
            response=make_response(
                results=(make_result(SearchResultKind.TOOL, 1, title=self.XSS),)
            )
        )
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_result_summary_is_escaped(self):
        html = self.html(
            response=make_response(
                results=(make_result(SearchResultKind.TOOL, 1, summary=self.XSS),)
            )
        )
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_template_uses_no_safe_filters(self):
        with open("search/templates/search/results.html", encoding="utf-8") as _f:
            source = _f.read()
        for unsafe in ("|safe", "mark_safe", "autoescape off"):
            with self.subTest(unsafe=unsafe):
                self.assertNotIn(unsafe, source)

    def test_summary_is_rendered_verbatim_without_re_truncation(self):
        summary = "A summary that already ends with the service ellipsis…"
        html = self.html(
            response=make_response(
                results=(make_result(SearchResultKind.TOOL, 1, summary=summary),)
            )
        )
        self.assertIn(summary, html)
        self.assertNotIn("...", html.split("editorial-card-intro")[1][:200])


class RobotsTests(SearchTemplateTestCase):
    def test_search_page_is_noindex_with_a_query(self):
        self.assertIn('<meta name="robots" content="noindex,follow">', self.html())

    def test_search_page_is_noindex_without_a_query(self):
        from search.query import SearchQueryIssue
        from search.tests.search_page_fixtures import empty_query_response

        html = self.html(
            "/en/search/", response=empty_query_response(SearchQueryIssue.EMPTY)
        )
        self.assertIn('<meta name="robots" content="noindex,follow">', html)

    def test_canonical_drops_the_query_string(self):
        html = self.html("/en/search/?q=ai%20tools")
        canonical = re.search(r'<link rel="canonical" href="([^"]+)"', html).group(1)
        self.assertTrue(canonical.endswith("/en/search/"))
