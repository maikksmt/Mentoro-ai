"""
Beta 10.2: the public comparison search must evaluate every search term
strictly in the requested language.

The Beta 10.1 audit proved on SQL level that ComparisonListView's search
chained its text lookups onto an ALREADY language-filtered queryset:

    Comparison.objects.visible_in_language(lang).filter(
        Q(translations__title__icontains=q) | ... | Q(tools__translations__name__icontains=q)
    )

Django opens a SEPARATE join for every filter() call that spans a
multi-valued relation, so the generated SQL contained exactly one
language_code condition - on the visibility join - while the search join
(alias T3) and the whole tools -> tool_translation chain stayed
language-unbounded:

    INNER JOIN compare_comparison_translation      ON id = master_id
    LEFT OUTER JOIN compare_comparison_translation T3 ON id = master_id   <- unbounded
    LEFT OUTER JOIN compare_comparisontoolentry / catalog_tool /
                    catalog_tool_translation                              <- unbounded
    WHERE ... AND compare_comparison_translation.language_code = 'de'
            AND (UPPER(T3.title) LIKE ... OR UPPER(catalog_tool_translation.name) LIKE ...)

A bilingual comparison whose search term only occurs in its ENGLISH text
therefore matched the GERMAN search and was then rendered with its German
title - a title not containing the search term at all. The same held for
linked tool names, and symmetrically in the other direction.

These tests reproduce that behaviour with real data (the audit only proved
the SQL shape) and pin the fix. They deliberately assert on behaviour, not
on SQL alias names, which are an implementation detail.

Every test resolves its language explicitly via translation.override() and
never relies on whatever language a previously executed test happened to
leave active.
"""
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone, translation

from catalog.models import Category, Tool
from compare.models import Comparison, ComparisonToolEntry
from compare.views import ComparisonListView
from core.models.editorial import EditorialWorkflowMixin

# Artificial tokens: no natural-language overlap, no substring collisions
# with the fixture boilerplate ("Intro", "Body", "Tool", ...).
EN_TOKEN = "Zylkomorph"
DE_TOKEN = "Quastenfeder"


def make_comparison(slug, *, texts, status=EditorialWorkflowMixin.STATUS_PUBLISHED,
                    published=True):
    """
    Creates a comparison with one translation per entry in `texts`.

    texts: {"en": {"title": ..., "intro": ..., "body": ...}, "de": {...}}
    Missing keys default to harmless boilerplate that contains neither token.
    """
    comparison = Comparison.objects.create(
        status=status,
        published_at=timezone.now() if published else None,
    )
    for language_code, values in texts.items():
        comparison.create_translation(
            language_code,
            title=values.get("title", f"Title {slug} {language_code}"),
            intro=values.get("intro", "Neutral intro"),
            body=values.get("body", "Neutral body"),
            slug=f"{slug}-{language_code}",
        )
    return comparison


def make_tool(slug, *, names):
    """Creates a tool with one translation per entry in `names` ({lang: name})."""
    tool = Tool.objects.create(slug=slug)
    for language_code, name in names.items():
        tool.create_translation(
            language_code,
            name=name,
            short_description="",
            long_description="",
        )
    return tool


def link(comparison, tool, position=0):
    return ComparisonToolEntry.objects.create(
        comparison=comparison, tool=tool, position=position
    )


class _SearchQuerySetMixin:
    """
    Runs ComparisonListView.get_queryset() for an explicit language and
    query, using RequestFactory + translation.override() (the convention
    established in compare/tests/test_views_public.py) so no ambient
    language state leaks in or out.
    """

    def search(self, language_code, query=None, **params):
        if query is not None:
            params["q"] = query
        with translation.override(language_code):
            request = RequestFactory().get(f"/{language_code}/compare/", params)
            view = ComparisonListView()
            view.request = request
            # Evaluate inside the override so any lazy translation work
            # happens under the requested language too.
            return list(view.get_queryset())

    def assertFinds(self, language_code, query, comparison, msg=""):
        pks = [obj.pk for obj in self.search(language_code, query)]
        self.assertIn(comparison.pk, pks, msg or f"{language_code}/{query} should match")

    def assertDoesNotFind(self, language_code, query, comparison, msg=""):
        pks = [obj.pk for obj in self.search(language_code, query)]
        self.assertNotIn(
            comparison.pk, pks, msg or f"{language_code}/{query} must not match"
        )


class ComparisonSearchTitleLanguageIsolationTests(_SearchQuerySetMixin, TestCase):
    """Pflichtfall 1 + 2: a title token must only ever match its own language."""

    @classmethod
    def setUpTestData(cls):
        cls.comparison = make_comparison(
            "title-isolation",
            texts={
                "en": {"title": f"English {EN_TOKEN} showdown"},
                "de": {"title": f"Deutscher {DE_TOKEN} Vergleich"},
            },
        )

    def test_english_only_title_token_does_not_match_german_search(self):
        # The regression: previously matched via the unbounded T3 join and
        # was then rendered with the German title, which has no EN_TOKEN.
        self.assertDoesNotFind("de", EN_TOKEN, self.comparison)

    def test_english_only_title_token_matches_english_search(self):
        self.assertFinds("en", EN_TOKEN, self.comparison)

    def test_german_only_title_token_does_not_match_english_search(self):
        self.assertDoesNotFind("en", DE_TOKEN, self.comparison)

    def test_german_only_title_token_matches_german_search(self):
        self.assertFinds("de", DE_TOKEN, self.comparison)


class ComparisonSearchTranslatedFieldsLanguageIsolationTests(
    _SearchQuerySetMixin, TestCase
):
    """
    Pflichtfall 3: the rule holds for every searched translated field,
    not just `title`.
    """

    SEARCHED_FIELDS = ("title", "intro", "body")

    def test_every_searched_field_is_language_isolated(self):
        for field in self.SEARCHED_FIELDS:
            with self.subTest(field=field):
                comparison = make_comparison(
                    f"field-isolation-{field}",
                    texts={
                        "en": {field: f"English {EN_TOKEN} text"},
                        "de": {field: f"Deutscher {DE_TOKEN} Text"},
                    },
                )
                self.assertFinds("en", EN_TOKEN, comparison)
                self.assertDoesNotFind("de", EN_TOKEN, comparison)
                self.assertFinds("de", DE_TOKEN, comparison)
                self.assertDoesNotFind("en", DE_TOKEN, comparison)


class ComparisonSearchToolNameLanguageIsolationTests(_SearchQuerySetMixin, TestCase):
    """
    Pflichtfall 4 + Beta 10.2 decision (H): linked tool names are searched
    ONLY in the requested language. Parler's English fallback must not act
    as a foreign-language search index.
    """

    @classmethod
    def setUpTestData(cls):
        cls.comparison = make_comparison(
            "tool-isolation",
            texts={"en": {}, "de": {}},  # neutral texts in both languages
        )
        cls.bilingual_tool = make_tool(
            "bilingual-tool",
            names={"en": f"{EN_TOKEN} Studio", "de": f"{DE_TOKEN} Studio"},
        )
        link(cls.comparison, cls.bilingual_tool)

    def test_english_tool_name_matches_english_search(self):
        self.assertFinds("en", EN_TOKEN, self.comparison)

    def test_english_tool_name_does_not_match_german_search(self):
        self.assertDoesNotFind("de", EN_TOKEN, self.comparison)

    def test_german_tool_name_matches_german_search(self):
        self.assertFinds("de", DE_TOKEN, self.comparison)

    def test_german_tool_name_does_not_match_english_search(self):
        self.assertDoesNotFind("en", DE_TOKEN, self.comparison)

    def test_untranslated_tool_does_not_match_via_parler_fallback(self):
        """
        A tool with an English translation only must not make a comparison
        match the German search - even though parler's fallback would
        happily *display* that English name on the German page.
        """
        comparison = make_comparison("fallback-tool", texts={"en": {}, "de": {}})
        english_only_tool = make_tool(
            "english-only-tool", names={"en": f"{EN_TOKEN} Fallback"}
        )
        link(comparison, english_only_tool)

        self.assertFinds("en", EN_TOKEN, comparison)
        self.assertDoesNotFind("de", EN_TOKEN, comparison)


class ComparisonSearchDuplicateTests(_SearchQuerySetMixin, TestCase):
    """Pflichtfall 5: multiple matching fields and tools yield exactly one row."""

    def test_comparison_matching_in_many_places_appears_once(self):
        comparison = make_comparison(
            "duplicate-check",
            texts={
                "en": {
                    "title": f"{EN_TOKEN} title",
                    "intro": f"{EN_TOKEN} intro",
                    "body": f"{EN_TOKEN} body",
                },
                "de": {},
            },
        )
        for index in range(3):
            link(
                comparison,
                make_tool(f"dup-tool-{index}", names={"en": f"{EN_TOKEN} Tool {index}"}),
                position=index,
            )

        results = self.search("en", EN_TOKEN)
        self.assertEqual(
            [obj.pk for obj in results].count(comparison.pk),
            1,
            "comparison must appear exactly once despite many matching fields/tools",
        )

    def test_queryset_count_matches_number_of_rows(self):
        comparison = make_comparison(
            "duplicate-count", texts={"en": {"title": f"{EN_TOKEN} x"}, "de": {}}
        )
        link(comparison, make_tool("dup-count-tool", names={"en": f"{EN_TOKEN} T"}))

        with translation.override("en"):
            request = RequestFactory().get("/en/compare/", {"q": EN_TOKEN})
            view = ComparisonListView()
            view.request = request
            queryset = view.get_queryset()
            self.assertEqual(queryset.count(), len(list(queryset)))


class ComparisonSearchVisibilityUnchangedTests(_SearchQuerySetMixin, TestCase):
    """
    Pflichtfall 6: the fix must not widen or narrow public visibility -
    only remove foreign-language-only matches.
    """

    def test_draft_comparison_never_matches(self):
        comparison = make_comparison(
            "draft-search",
            texts={"en": {"title": f"{EN_TOKEN} draft"}, "de": {"title": f"{DE_TOKEN} Entwurf"}},
            status=EditorialWorkflowMixin.STATUS_DRAFT,
            published=False,
        )
        self.assertDoesNotFind("en", EN_TOKEN, comparison)
        self.assertDoesNotFind("de", DE_TOKEN, comparison)

    def test_published_comparison_still_matches_in_its_own_language(self):
        comparison = make_comparison(
            "published-search", texts={"en": {"title": f"{EN_TOKEN} live"}}
        )
        self.assertFinds("en", EN_TOKEN, comparison)

    def test_comparison_without_translation_in_language_never_matches(self):
        """An EN-only comparison stays invisible on the German list, search or not."""
        comparison = make_comparison(
            "en-only-search", texts={"en": {"title": f"{EN_TOKEN} solo"}}
        )
        self.assertDoesNotFind("de", EN_TOKEN, comparison)
        self.assertDoesNotFind("de", "solo", comparison)

    def test_matched_results_expose_a_language_correct_public_url(self):
        comparison = make_comparison(
            "url-safety",
            texts={"en": {"title": f"{EN_TOKEN} url"}, "de": {"title": f"{DE_TOKEN} url"}},
        )
        for language_code, token in (("en", EN_TOKEN), ("de", DE_TOKEN)):
            with self.subTest(language=language_code):
                results = self.search(language_code, token)
                self.assertIn(comparison.pk, [obj.pk for obj in results])
                with translation.override(language_code):
                    url = comparison.get_absolute_url(language=language_code)
                self.assertNotEqual(url, "#")
                self.assertTrue(url.startswith(f"/{language_code}/compare/"), url)
                self.assertIn(f"url-safety-{language_code}", url)

    def test_empty_query_returns_the_unfiltered_public_list(self):
        make_comparison("empty-query-a", texts={"en": {}})
        make_comparison("empty-query-b", texts={"en": {}})
        self.assertEqual(
            len(self.search("en", "")),
            Comparison.objects.visible_in_language("en").count(),
        )


class ComparisonSearchCategoryCombinationTests(_SearchQuerySetMixin, TestCase):
    """Pflichtfall 7: search and the existing category filter still cooperate."""

    @classmethod
    def setUpTestData(cls):
        cls.category = Category.objects.create()
        cls.category.create_translation("en", name="Chatbots", slug="chatbots-en")
        cls.category.create_translation("de", name="Chatbots", slug="chatbots-de")

        cls.other_category = Category.objects.create()
        cls.other_category.create_translation("en", name="Vision", slug="vision-en")

        cls.tool = make_tool("category-tool", names={"en": "Category Tool"})
        cls.tool.categories.add(cls.category)

        cls.matching = make_comparison(
            "category-match", texts={"en": {"title": f"{EN_TOKEN} categorized"}}
        )
        link(cls.matching, cls.tool)

        # Same category, but no search-token match.
        cls.other = make_comparison("category-other", texts={"en": {"title": "Unrelated"}})
        link(cls.other, cls.tool)

    def test_search_and_category_combine(self):
        results = self.search("en", EN_TOKEN, category="chatbots-en")
        pks = [obj.pk for obj in results]
        self.assertIn(self.matching.pk, pks)
        self.assertNotIn(self.other.pk, pks)

    def test_category_alone_is_unchanged(self):
        results = self.search("en", category="chatbots-en")
        pks = [obj.pk for obj in results]
        self.assertIn(self.matching.pk, pks)
        self.assertIn(self.other.pk, pks)

    def test_unknown_category_yields_no_results(self):
        self.assertEqual(self.search("en", category="does-not-exist"), [])

    def test_category_without_search_token_match_excludes_everything(self):
        self.assertEqual(self.search("en", EN_TOKEN, category="vision-en"), [])


class ComparisonSearchPaginationTests(TestCase):
    """
    Pflichtfall 8: pagination behaviour around the search term is untouched
    (paginate_by, `q` preservation, no duplicated `page`).
    """

    @classmethod
    def setUpTestData(cls):
        for index in range(20):
            make_comparison(
                f"pagination-{index}", texts={"en": {"title": f"{EN_TOKEN} item {index}"}}
            )

    def setUp(self):
        # self.client.get() runs LocaleMiddleware, which activates a language
        # and never restores the previous one (see the same guard in
        # compare/tests/test_views_public.py).
        self.addCleanup(translation.deactivate_all)

    def test_paginate_by_is_unchanged(self):
        self.assertEqual(ComparisonListView.paginate_by, 15)

    def test_search_results_are_paginated(self):
        response = self.client.get(reverse("compare:index"), {"q": EN_TOKEN})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["paginator"].count, 20)
        self.assertEqual(len(response.context["objects"]), 15)

    def test_pagination_link_preserves_the_search_term(self):
        response = self.client.get(reverse("compare:index"), {"q": EN_TOKEN})
        html = response.content.decode()
        self.assertIn(f"q={EN_TOKEN}", html)

    def test_second_page_still_applies_the_search_filter(self):
        response = self.client.get(reverse("compare:index"), {"q": EN_TOKEN, "page": 2})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["paginator"].count, 20)
        self.assertEqual(len(response.context["objects"]), 5)

    def test_pagination_links_do_not_duplicate_the_page_parameter(self):
        response = self.client.get(reverse("compare:index"), {"q": EN_TOKEN})
        html = response.content.decode()
        for href in _pagination_hrefs(html):
            self.assertEqual(href.count("page="), 1, href)


def _pagination_hrefs(html):
    import re

    return re.findall(r'<a href="(\?page=[^"]+)"', html)
