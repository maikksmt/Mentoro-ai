"""
Beta 10.5: what is specific to the comparison adapter.

Comparisons expose only strictly published objects, and their linked tool
names are deliberately not searchable yet. This module also confirms the
adapter is independent of the public comparison list search hardened in
Beta 10.2.
"""
from unittest import skipUnless

from django.db import connection
from django.test import RequestFactory, TestCase
from django.utils import translation

from catalog.models import Tool
from compare.models import Comparison, ComparisonToolEntry
from compare.views import ComparisonListView
from search.adapters.comparisons import (
    COMPARISON_SEARCH_FIELDS,
    ComparisonSearchAdapter,
)
from search.query import normalize_search_query
from search.result_types import SearchResultKind
from search.tests.editorial_fixtures import (
    ADAPTER_SPECS,
    begin_unpublished_revision,
    make_author,
    publish,
)

postgresql_only = skipUnless(
    connection.vendor == "postgresql", "PostgreSQL full-text search required"
)

COMPARISON_SPEC = next(spec for spec in ADAPTER_SPECS if spec.name == "comparison")


class ComparisonAdapterTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = make_author("comparison-adapter-editor")

    def setUp(self):
        self.adapter = ComparisonSearchAdapter()

    def search(self, term, language_code="en"):
        return self.adapter.search(
            query=normalize_search_query(term), language_code=language_code
        )

    def ids(self, term, language_code="en"):
        return [r.object_id for r in self.search(term, language_code)]

    def make(self, slug, **values):
        payload = {
            "title": "Neutral comparison heading",
            "intro": "neutral intro",
            "body": "neutral body",
            "slug": slug,
        }
        payload.update(values)
        return publish(COMPARISON_SPEC, author=self.author, translations={"en": payload})


class ComparisonFieldConfigurationTests(TestCase):
    def test_indexes_title_intro_and_body_only(self):
        self.assertEqual(
            [f.public_field for f in COMPARISON_SEARCH_FIELDS],
            ["title", "intro", "body"],
        )

    def test_no_outro_field_exists_on_the_model(self):
        self.assertNotIn(
            "outro",
            [f.name for f in Comparison._parler_meta.root_model._meta.get_fields()],
        )

    def test_adapter_kind(self):
        self.assertIs(ComparisonSearchAdapter.kind, SearchResultKind.COMPARISON)


@postgresql_only
class ComparisonToolMetadataTests(ComparisonAdapterTestCase):
    """Tool names are deliberately deferred to the tool adapter slice."""

    def _with_tool(self, slug, tool_names):
        comparison = self.make(slug)
        tool = Tool.objects.create(slug=f"{slug}-tool")
        for language_code, name in tool_names.items():
            tool.create_translation(
                language_code, name=name, short_description="", long_description=""
            )
        ComparisonToolEntry.objects.create(comparison=comparison, tool=tool)
        return comparison, tool

    def test_linked_tool_name_is_not_searchable(self):
        comparison, _ = self._with_tool(
            "cmp-tool-en", {"en": "Tooltoken Studio", "de": "Tooltoken Studio"}
        )
        self.assertNotIn(comparison.pk, self.ids("Tooltoken"))

    def test_comparison_with_tools_is_still_findable_by_its_own_text(self):
        comparison, _ = self._with_tool(
            "cmp-tool-own-en", {"en": "Some Tool"}
        )
        self.assertIn(comparison.pk, self.ids("Neutral"))

    def test_tools_do_not_duplicate_results(self):
        comparison = self.make("cmp-multitool-en", title="Multitooltoken comparison")
        for index in range(3):
            tool = Tool.objects.create(slug=f"cmp-multitool-{index}")
            tool.create_translation(
                "en", name=f"Tool {index}", short_description="", long_description=""
            )
            ComparisonToolEntry.objects.create(comparison=comparison, tool=tool)
        self.assertEqual(self.ids("Multitooltoken").count(comparison.pk), 1)


@postgresql_only
class ComparisonVisibilityTests(ComparisonAdapterTestCase):
    def test_review_with_live_revision_is_not_public(self):
        # ComparisonQuerySet.visible_in_language() uses published().
        comparison = self.make("cmp-review-en", title="Reviewtoken comparison")
        self.assertIn(comparison.pk, self.ids("Reviewtoken"))

        begin_unpublished_revision(
            comparison, author=self.author, language_code="en", intro="edited"
        )
        self.assertNotIn(comparison.pk, self.ids("Reviewtoken"))
        self.assertNotIn(
            comparison.pk,
            list(
                Comparison.objects.visible_in_language("en").values_list("pk", flat=True)
            ),
        )

    def test_search_matches_the_public_queryset(self):
        self.make("cmp-parity-a-en", title="Paritytoken one")
        self.make("cmp-parity-b-en", title="Paritytoken two")
        public_ids = set(
            Comparison.objects.visible_in_language("en").values_list("pk", flat=True)
        )
        self.assertTrue(set(self.ids("Paritytoken")).issubset(public_ids))


@postgresql_only
class ComparisonListSearchUnaffectedTests(ComparisonAdapterTestCase):
    """The Beta 10.2 list search and this adapter are independent."""

    def _list_queryset_ids(self, term, language_code):
        with translation.override(language_code):
            request = RequestFactory().get(f"/{language_code}/compare/", {"q": term})
            view = ComparisonListView()
            view.request = request
            return [obj.pk for obj in view.get_queryset()]

    def test_list_search_still_matches_its_own_language_only(self):
        comparison = publish(
            COMPARISON_SPEC,
            author=self.author,
            translations={
                "en": {
                    "title": "Listtoken comparison",
                    "intro": "i",
                    "body": "b",
                    "slug": "cmp-list-en",
                },
                "de": {
                    "title": "Deutscher Vergleich",
                    "intro": "i",
                    "body": "b",
                    "slug": "cmp-list-de",
                },
            },
        )
        self.assertIn(comparison.pk, self._list_queryset_ids("Listtoken", "en"))
        self.assertNotIn(comparison.pk, self._list_queryset_ids("Listtoken", "de"))

    def test_list_search_uses_substring_while_the_adapter_uses_full_text(self):
        # The list search still matches partial words; the adapter stems
        # instead. Both are correct for their surface - this pins that the
        # adapter did not change the list's behaviour.
        comparison = self.make("cmp-substring-en", title="Substringtoken comparison")
        self.assertIn(comparison.pk, self._list_queryset_ids("ubstringtok", "en"))
        self.assertNotIn(comparison.pk, self.ids("ubstringtok"))
