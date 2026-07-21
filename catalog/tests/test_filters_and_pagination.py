"""
Beta 9.8: catalog search/filter UX and the shared pagination partial.

Existing filter semantics (q/free/category params, ToolQuerySet.public(),
paginate_by=15) are untouched - these tests pin the *presentation* fixes:
a real <label for> on every control (no placeholder-only labelling), a
non-interactive active-filter summary with an unambiguous "clear all"
escape hatch, a distinct filtered-vs-genuinely-empty empty state, and a
pagination partial that preserves existing query parameters, replaces
(never duplicates) `page`, marks the current page with aria-current, and
never renders a disabled Previous/Next as a real, focusable link.

Deliberately avoids asserting on full Tailwind class strings - only on the
stable `catalog-*`/`active-filter-*`/`pagination-*` structure classes and
on real HTTP/query-string behaviour.
"""
import re
from html.parser import HTMLParser

from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone, translation

from catalog.models import Category, Tool
from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin


class _NestingChecker(HTMLParser):
    """Detects genuine nesting (not merely sibling tags) of a given tag."""

    def __init__(self, tag):
        super().__init__()
        self.tag = tag
        self.depth = 0
        self.found_nested = False

    def handle_starttag(self, tag, attrs):
        if tag == self.tag:
            if self.depth:
                self.found_nested = True
            self.depth += 1

    def handle_endtag(self, tag):
        if tag == self.tag and self.depth:
            self.depth -= 1


def _has_nested(html, tag):
    checker = _NestingChecker(tag)
    checker.feed(html)
    return checker.found_nested


def _pagination_link_href(html):
    """Finds a real (non-disabled) pagination <a> tag regardless of
    attribute order and returns its href, or None if none exists."""
    for tag in re.findall(r"<a\b[^>]*>", html):
        if "pagination-link" in tag:
            href = re.search(r'href="([^"]*)"', tag)
            if href:
                return href.group(1)
    return None


def _catalog_filter_form(html):
    """The page has more than one <form> (e.g. the navbar language
    switcher) - return the one that actually holds the catalog filters."""
    start = html.index('class="catalog-filter-grid"')
    form_start = html.rindex("<form", 0, start)
    form_end = html.index(">", form_start) + 1
    return html[form_start:form_end]


def make_tool(*, slug, categories=(), free_tier=False):
    t = Tool.objects.create(slug=slug, published_at=timezone.now(), free_tier=free_tier)
    with translation.override("en"):
        t.create_translation("en", name=f"Tool {slug}", short_description="s")
    for cat in categories:
        t.categories.add(cat)
    return t


def make_category(*, slug):
    c = Category.objects.create()
    with translation.override("en"):
        c.create_translation("en", name=f"Category {slug}", slug=f"{slug}-en")
    return c


class CatalogUITestCase(TestCase):
    """
    Restores the ambient language after each test.

    ``self.client.get()`` runs LocaleMiddleware, which activates the language
    of the request and never deactivates it again. The assertions below are
    written against the English UI copy, so whichever language the previously
    scheduled test happened to request stays active and decides whether they
    pass - which is why they failed under ``--shuffle`` on some seeds and not
    on others.

    Activating on the way *in* is what actually fixes it. Cleaning up on the
    way out only disciplines this module, while the language that broke these
    tests is activated by an entirely different one - catalog's own
    test_language_fallback requests /de/ pages. These tests build their URLs
    with ``reverse()``, which reads the active language to pick the i18n
    prefix, so a leaked "de" sends them to the German page and every English
    assertion fails. Declaring the language here makes them independent of
    whatever ran before.

    The language is *activated*, not deactivated: ``deactivate_all()`` leaves
    no active language at all, and parler refuses to build a translated model
    without one, so the next class's ``setUpTestData`` dies with
    "language_code can't be null".
    """

    def setUp(self):
        super().setUp()
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)


class CatalogFilterFormStructureTests(CatalogUITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cat = make_category(slug="filter-struct")
        make_tool(slug="filter-struct-tool", categories=[cls.cat])

    def _get(self, **params):
        return self.client.get(reverse("catalog:list"), params)

    def test_form_uses_get_method(self):
        html = self._get().content.decode()
        form_tag = _catalog_filter_form(html)
        self.assertIn('method="get"', form_tag)

    def test_search_input_has_a_real_associated_label(self):
        html = self._get().content.decode()
        self.assertRegex(html, r'<label[^>]*for="catalog-search-input"[^>]*>')
        self.assertIn('id="catalog-search-input"', html)

    def test_free_tier_checkbox_has_a_real_associated_label(self):
        html = self._get().content.decode()
        self.assertRegex(html, r'<label[^>]*for="catalog-free-tier"[^>]*>')
        self.assertIn('id="catalog-free-tier"', html)

    def test_existing_parameter_names_are_unchanged(self):
        resp = self._get(q="tool", free="1", category=self.cat.safe_translation_getter("slug"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        # The values the view actually resolved must round-trip into the form.
        self.assertIn('name="q"', html)
        self.assertIn('name="free"', html)
        self.assertIn('name="category"', html)

    def test_filter_form_does_not_carry_a_stale_page_field(self):
        html = self._get(page=2).content.decode()
        form_start = html.index("<form")
        form_end = html.index("</form>", form_start)
        form_html = html[form_start:form_end]
        self.assertNotIn('name="page"', form_html)

    def test_reset_link_is_a_plain_link_without_role_or_aria_pressed(self):
        html = self._get(q="tool").content.decode()
        # Beta 9.9 added `.touch-target` alongside `.catalog-filter-reset`
        # for mobile touch sizing - match on the stable class token rather
        # than the exact class attribute value.
        reset = re.search(r'<a[^>]*class="[^"]*catalog-filter-reset[^"]*"[^>]*>', html).group(0)
        self.assertNotIn("role=", reset)
        self.assertNotIn("aria-pressed", reset)
        self.assertIn('href="."', reset)


class CatalogActiveFiltersTests(CatalogUITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cat = make_category(slug="active-filter")
        make_tool(slug="active-filter-tool", categories=[cls.cat], free_tier=True)

    def _get(self, **params):
        return self.client.get(reverse("catalog:list"), params)

    def test_no_active_filter_summary_without_any_filter(self):
        html = self._get().content.decode()
        self.assertNotIn("active-filter-list", html)

    def test_active_search_term_is_shown(self):
        html = self._get(q="hello world").content.decode()
        self.assertIn("active-filter-list", html)
        self.assertIn("hello world", html)

    def test_active_search_term_is_html_escaped(self):
        html = self._get(q="<script>alert(1)</script>").content.decode()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_active_category_name_is_shown(self):
        cat_slug = self.cat.safe_translation_getter("slug", language_code="en")
        html = self._get(category=cat_slug).content.decode()
        self.assertIn("active-filter-list", html)
        self.assertIn(self.cat.safe_translation_getter("name", language_code="en"), html)

    def test_free_tier_filter_is_shown(self):
        html = self._get(free="1").content.decode()
        self.assertIn("active-filter-list", html)

    def test_clear_all_link_removes_every_filter(self):
        html = self._get(q="x", free="1", category="whatever").content.decode()
        clear_all = re.search(r'<a[^>]*class="active-filter-clear"[^>]*>', html).group(0)
        self.assertIn('href="."', clear_all)
        resp = self.client.get(reverse("catalog:list"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("active-filter-list", resp.content.decode())

    def test_active_filter_chips_are_not_interactive(self):
        html = self._get(q="x").content.decode()
        chips = re.findall(r'<span class="active-filter-chip">.*?</span>', html)
        self.assertTrue(chips)
        for chip in chips:
            self.assertNotIn("<button", chip)
            self.assertNotIn("<a ", chip)

    def test_clear_all_is_a_real_link_distinct_from_the_chips(self):
        html = self._get(q="x").content.decode()
        clear_all = re.search(r'<a[^>]*class="active-filter-clear"[^>]*>', html)
        self.assertIsNotNone(clear_all)


class CatalogResultsSummaryAndEmptyStateTests(CatalogUITestCase):
    @classmethod
    def setUpTestData(cls):
        make_tool(slug="summary-tool-1")
        make_tool(slug="summary-tool-2")

    def _get(self, **params):
        return self.client.get(reverse("catalog:list"), params)

    def test_results_summary_uses_real_paginator_count(self):
        html = self._get().content.decode()
        self.assertIn("2 tools found", html)

    def test_results_summary_correct_in_singular(self):
        html = self._get(q="summary-tool-1").content.decode()
        self.assertIn("1 tool found", html)

    def test_genuinely_empty_state_shows_generic_message(self):
        Tool.objects.all().delete()
        html = self._get().content.decode()
        self.assertIn("No tools found.", html)
        self.assertNotIn("No tools match your current search", html)

    def test_filtered_empty_state_is_distinct_and_offers_reset(self):
        html = self._get(q="no-such-tool-xyz").content.decode()
        self.assertIn("No tools match your current search or filters.", html)
        self.assertIn("Clear filters and see all tools", html)
        empty_start = html.index("catalog-empty-state")
        empty_end = html.index("</div>", empty_start)
        self.assertIn('href="."', html[empty_start:empty_end])

    def test_results_summary_is_correct_at_zero(self):
        html = self._get(q="no-such-tool-xyz").content.decode()
        self.assertIn("0 tools found", html)


class PaginationQueryPreservationTests(CatalogUITestCase):
    """Catalog has enough tools to force a second page; the pagination
    partial must preserve q/category/free and replace (not duplicate)
    page."""

    @classmethod
    def setUpTestData(cls):
        cls.cat = make_category(slug="pagination-cat")
        for i in range(20):
            make_tool(slug=f"pagination-tool-{i}", categories=[cls.cat], free_tier=True)

    def test_pagination_preserves_free_and_category_on_next_link(self):
        cat_slug = self.cat.safe_translation_getter("slug", language_code="en")
        resp = self.client.get(
            reverse("catalog:list"), {"free": "1", "category": cat_slug}
        )
        html = resp.content.decode()
        href = _pagination_link_href(html)
        self.assertIsNotNone(href)
        self.assertIn("page=2", href)
        self.assertIn("free=1", href)
        self.assertIn(f"category={cat_slug}", href)

    def test_page_parameter_is_replaced_not_duplicated(self):
        resp = self.client.get(reverse("catalog:list"), {"free": "1", "page": 2})
        html = resp.content.decode()
        href = _pagination_link_href(html)
        self.assertIsNotNone(href)
        self.assertEqual(href.count("page="), 1)

    def test_current_page_has_aria_current(self):
        resp = self.client.get(reverse("catalog:list"), {"free": "1", "page": 2})
        html = resp.content.decode()
        current = re.search(r'<span[^>]*class="pagination-current[^"]*"[^>]*>', html).group(0)
        self.assertIn('aria-current="page"', current)

    def test_disabled_previous_is_not_a_link(self):
        resp = self.client.get(reverse("catalog:list"), {"free": "1"})
        html = resp.content.decode()
        disabled = re.search(r'<span[^>]*class="pagination-link pagination-disabled[^"]*"[^>]*>', html)
        self.assertIsNotNone(disabled)
        self.assertNotIn("href", disabled.group(0))

    def test_disabled_previous_is_not_focusable(self):
        resp = self.client.get(reverse("catalog:list"), {"free": "1"})
        html = resp.content.decode()
        disabled = re.search(r'<span[^>]*class="pagination-link pagination-disabled[^"]*"[^>]*>', html).group(0)
        self.assertNotIn("tabindex", disabled)

    def test_pagination_nav_has_localized_aria_label(self):
        resp = self.client.get(reverse("catalog:list"), {"free": "1"})
        html = resp.content.decode()
        self.assertIn('aria-label="Pagination"', html)

    def test_no_nested_interactive_elements_in_pagination(self):
        resp = self.client.get(reverse("catalog:list"), {"free": "1"})
        html = resp.content.decode()
        nav_start = html.index("pagination-shell")
        nav_end = html.index("</nav>", nav_start)
        segment = html[html.rindex("<nav", 0, nav_start):nav_end]
        self.assertFalse(_has_nested(segment, "a"))
        self.assertNotIn("<button", segment)


class ComparisonListPaginationRegressionTests(CatalogUITestCase):
    """Beta 9.8 bug fix: comparison_list.html passed its search term to the
    shared pagination partial under the context name `q`, but the partial
    reads `query` - so pagination silently dropped the active search term
    (category, which happens to share its name, was unaffected)."""

    @classmethod
    def setUpTestData(cls):
        with translation.override("en"):
            for i in range(20):
                c = Comparison.objects.create(
                    status=EditorialWorkflowMixin.STATUS_PUBLISHED,
                    published_at=timezone.now(),
                )
                c.create_translation(
                    "en", title=f"Regression Comparison {i}", intro="i", body="b",
                    slug=f"pagination-regression-{i}-en",
                )

    def test_pagination_preserves_the_search_term(self):
        resp = self.client.get(reverse("compare:index"), {"q": "regression"})
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        href = _pagination_link_href(html)
        self.assertIsNotNone(href, "expected a paginated result set")
        self.assertIn("q=regression", href)

    def test_reset_link_has_no_role_or_aria_pressed(self):
        resp = self.client.get(reverse("compare:index"), {"q": "regression"})
        html = resp.content.decode()
        reset = re.search(r'<a[^>]*aria-label="Reset filters"[^>]*>', html).group(0)
        self.assertNotIn("role=", reset)
        self.assertNotIn("aria-pressed", reset)


class OtherPaginatedListsUnaffectedTests(CatalogUITestCase):
    """Guides/prompts/usecases have no filters at all - the shared
    partial must keep paginating them correctly with only `page` set."""

    def test_guides_pagination_has_no_stray_filter_params(self):
        from guides.models import Guide
        with translation.override("en"):
            for i in range(20):
                g = Guide.objects.create(
                    status=EditorialWorkflowMixin.STATUS_PUBLISHED,
                    published_at=timezone.now(),
                )
                g.create_translation("en", slug=f"pg-guide-{i}-en", title=f"G{i}", intro="i", body="b")
        resp = self.client.get(reverse("guides:list"))
        html = resp.content.decode()
        href = _pagination_link_href(html)
        self.assertIsNotNone(href)
        self.assertEqual(href, "?page=2")


class GlobalSearchUnaffectedTests(CatalogUITestCase):
    """The catalog's own filter form and the global search are separate
    controls; neither slice may absorb the other."""

    def test_global_search_link_is_present_and_distinct_from_the_filter_form(self):
        html = self.client.get("/en/catalog/").content.decode()
        self.assertIn('id="global-search-link"', html)
        self.assertIn('href="/en/search/"', html)
        # The catalog form still filters the catalog, not the whole site.
        form = re.search(r'<form[^>]*catalog-filter.*?</form>', html, re.DOTALL)
        self.assertIsNotNone(form, "catalog filter form not found")
        self.assertNotIn("/en/search/", form.group(0))


class CatalogTouchTargetTests(CatalogUITestCase):
    """Beta 9.9: the filter Search/Reset controls and a tool card's
    external Website link were sized below a practical ~40px mobile touch
    target (DaisyUI's `.btn-sm`/no size modifier). Fixed with the small,
    scoped `.touch-target` utility (min-height/min-width only) rather than
    touching `.btn`/`.btn-sm` globally - filter params/semantics untouched."""

    @classmethod
    def setUpTestData(cls):
        cls.cat = make_category(slug="touch-target")
        cls.tool = make_tool(slug="touch-target-tool", categories=[cls.cat])
        cls.tool.website = "https://example.com/touch-target-tool"
        cls.tool.save()

    def test_filter_search_button_has_touch_target_class(self):
        resp = self.client.get(reverse("catalog:list"))
        html = resp.content.decode()
        button = re.search(r'<button class="[^"]*touch-target[^"]*"\s+aria-label="Search">', html)
        self.assertIsNotNone(button, "catalog filter Search button is missing .touch-target")

    def test_filter_reset_link_has_touch_target_class(self):
        resp = self.client.get(reverse("catalog:list"), {"q": "x"})
        html = resp.content.decode()
        reset = re.search(r'<a\s+href="\."\s+class="catalog-filter-reset touch-target"', html)
        self.assertIsNotNone(reset, "catalog filter Reset link is missing .touch-target")

    def test_tool_card_website_link_is_not_undersized(self):
        resp = self.client.get(reverse("catalog:list"))
        html = resp.content.decode()
        website = re.search(r'<a href="https://example\.com/touch-target-tool"[^>]*class="([^"]*)"', html)
        self.assertIsNotNone(website)
        self.assertNotIn("btn-sm", website.group(1))
