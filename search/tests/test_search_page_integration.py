"""
Beta 10.8: the search page against the real service and all five adapters.

Requires PostgreSQL.
"""
from datetime import timedelta
from unittest import skipUnless

from django.conf import settings
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone, translation

from catalog.models import Tool
from core.models.editorial import EditorialWorkflowMixin
from search.registry import SEARCH_ADAPTERS
from search.tests.editorial_fixtures import (
    ADAPTER_SPECS,
    edit_without_publishing,
    make_author,
    publish,
)

postgresql_only = skipUnless(
    connection.vendor == "postgresql", "PostgreSQL full-text search required"
)

PAST = timedelta(days=1)
FUTURE = timedelta(days=30)
EDITORIAL_SPECS = {spec.name: spec for spec in ADAPTER_SPECS}
EDITORIAL_NAMES = ("guide", "prompt", "usecase", "comparison")


def make_tool(slug, *, translations, published_at=None):
    tool = Tool.objects.create(
        slug=slug, published_at=published_at or timezone.now() - PAST
    )
    for language_code, values in translations.items():
        tool.create_translation(
            language_code,
            name=values.get("name", "Neutral tool"),
            short_description=values.get("short_description", ""),
            long_description=values.get("long_description", ""),
        )
    return tool


class SearchPageIntegrationTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = make_author("search-page-editor")

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)

    def make_editorial(self, name, slug, *, title, texts=None):
        payload = {"title": title, "slug": slug}
        payload.update(texts or {})
        return publish(
            EDITORIAL_SPECS[name], author=self.author, translations={"en": payload}
        )


@postgresql_only
class MixedResultsTests(SearchPageIntegrationTestCase):
    def test_all_five_content_types_appear(self):
        for name in EDITORIAL_NAMES:
            self.make_editorial(name, f"page-{name}-en", title="Pagetoken heading")
        make_tool("page-tool", translations={"en": {"name": "Pagetoken heading"}})

        response = self.client.get("/en/search/", {"q": "Pagetoken"})
        html = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("5 results for", html)
        for label in ("Tools", "Guides", "Prompts", "Use cases", "Comparisons"):
            with self.subTest(label=label):
                self.assertIn(f"{label}: 1", html)

    def test_page_preserves_the_service_order(self):
        for name in EDITORIAL_NAMES:
            self.make_editorial(name, f"order-{name}-en", title="Ordertoken heading")
        make_tool("order-tool", translations={"en": {"name": "Ordertoken heading"}})

        from search.services import search_site

        expected = [
            f"search-result-{result.kind}-{result.object_id}"
            for result in search_site(
                raw_query="Ordertoken", language_code="en"
            ).results
        ]
        html = self.client.get("/en/search/", {"q": "Ordertoken"}).content.decode()
        positions = [html.index(f'id="{marker}"') for marker in expected]
        self.assertEqual(positions, sorted(positions))

    def test_result_links_resolve(self):
        for name in EDITORIAL_NAMES:
            self.make_editorial(name, f"link-{name}-en", title="Linktoken heading")
        make_tool("link-tool", translations={"en": {"name": "Linktoken heading"}})

        from search.services import search_site

        for result in search_site(raw_query="Linktoken", language_code="en").results:
            with self.subTest(kind=result.kind):
                self.assertEqual(self.client.get(result.url).status_code, 200)


@postgresql_only
class LanguageIsolationTests(SearchPageIntegrationTestCase):
    def test_page_shows_only_the_requested_language(self):
        for name in EDITORIAL_NAMES:
            publish(
                EDITORIAL_SPECS[name],
                author=self.author,
                translations={
                    "en": {"title": "Englishonly heading", "slug": f"iso-{name}-en"},
                    "de": {"title": "Deutschonly Titel", "slug": f"iso-{name}-de"},
                },
            )
        make_tool(
            "iso-tool",
            translations={
                "en": {"name": "Englishonly heading"},
                "de": {"name": "Deutschonly Titel"},
            },
        )

        english = self.client.get("/en/search/", {"q": "Englishonly"}).content.decode()
        self.assertIn("5 results for", english)
        german_for_english = self.client.get(
            "/de/search/", {"q": "Englishonly"}
        ).content.decode()
        self.assertIn("Keine Ergebnisse für", german_for_english)

        german = self.client.get("/de/search/", {"q": "Deutschonly"}).content.decode()
        self.assertIn("5 Ergebnisse für", german)

    def test_english_only_tool_does_not_leak_into_german(self):
        make_tool("leak-tool", translations={"en": {"name": "Fallbacktoken Studio"}})
        self.assertIn(
            "1 result for",
            self.client.get("/en/search/", {"q": "Fallbacktoken"}).content.decode(),
        )
        self.assertIn(
            "Keine Ergebnisse für",
            self.client.get("/de/search/", {"q": "Fallbacktoken"}).content.decode(),
        )

    def test_editorial_draft_does_not_leak(self):
        guide = self.make_editorial(
            "guide", "leak-guide-en", title="Publishedtoken heading"
        )
        edit_without_publishing(guide, language_code="en", title="Draftneedle heading")
        self.assertIn(
            "1 result for",
            self.client.get("/en/search/", {"q": "Publishedtoken"}).content.decode(),
        )
        self.assertIn(
            "No results for",
            self.client.get("/en/search/", {"q": "Draftneedle"}).content.decode(),
        )


@postgresql_only
class VisibilityTests(SearchPageIntegrationTestCase):
    def test_future_tool_is_not_shown(self):
        make_tool(
            "future-page-tool",
            published_at=timezone.now() + FUTURE,
            translations={"en": {"name": "Scheduledtoken Studio"}},
        )
        self.assertIn(
            "No results for",
            self.client.get("/en/search/", {"q": "Scheduledtoken"}).content.decode(),
        )

    def test_draft_editorial_object_is_not_shown(self):
        for name in EDITORIAL_NAMES:
            spec = EDITORIAL_SPECS[name]
            obj = spec.model.objects.create(status=EditorialWorkflowMixin.STATUS_DRAFT)
            obj.create_translation(
                "en",
                title="Draftstatustoken heading",
                slug=f"page-draft-{name}-en",
                **{field: "" for field in spec.text_fields},
                **spec.required_extra,
            )
        self.assertIn(
            "No results for",
            self.client.get("/en/search/", {"q": "Draftstatustoken"}).content.decode(),
        )


@postgresql_only
class QueryCountTests(SearchPageIntegrationTestCase):
    """
    Measures what the *search* costs, not what the page costs.

    Every page in this project runs the global public_inventory context
    processor, which counts tools, categories and each editorial type for the
    footer and then stores the result in the DatabaseCache - a dozen
    statements on a cold cache, entirely independent of this view. Comparing
    against a baseline request that performs no search isolates the adapters'
    cost and stays correct if the site chrome changes.
    """

    def _count(self, params):
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get("/en/search/", params)
        self.assertEqual(response.status_code, 200)
        return len(captured.captured_queries)

    def _baseline(self):
        # Warm the inventory cache first so the baseline excludes its one-off
        # population, then measure a request that does no search at all.
        self.client.get("/en/search/")
        return self._count({})

    def test_a_search_costs_exactly_one_query_per_adapter(self):
        for name in EDITORIAL_NAMES:
            self.make_editorial(name, f"qc-{name}-en", title="Countingtoken heading")
        make_tool("qc-tool", translations={"en": {"name": "Countingtoken heading"}})

        baseline = self._baseline()
        with_search = self._count({"q": "Countingtoken"})
        self.assertEqual(
            with_search - baseline,
            len(SEARCH_ADAPTERS),
            "expected one query per adapter and none for counts or rendering",
        )

    def test_invalid_queries_cost_nothing_beyond_the_page_itself(self):
        baseline = self._baseline()
        for params in ({}, {"q": ""}, {"q": "a"}, {"q": "x" * 120}):
            with self.subTest(params=params):
                self.assertEqual(self._count(params), baseline)
