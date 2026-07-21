"""
Beta 10.7: the global search service against all five real adapters.

Complements the database-free service tests: those pin the service's own
contract with fakes, these prove the five real adapters actually cooperate -
same language, same visibility rules, one query each, and results that are
comparable enough to sort together.

Requires PostgreSQL.
"""
from datetime import timedelta
from unittest import skipUnless

from django.db import connection
from django.conf import settings
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone, translation

from catalog.models import Tool
from core.models.editorial import EditorialWorkflowMixin
from search.query import SearchQueryIssue
from search.registry import SEARCH_ADAPTERS
from search.result_types import SearchResult, SearchResultKind
from search.services import search_site
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
NEUTRAL = "Nothing relevant in this sentence."

EDITORIAL_SPECS = {spec.name: spec for spec in ADAPTER_SPECS}


def make_tool(slug, *, translations, vendor="", published_at=None):
    tool = Tool.objects.create(
        slug=slug, vendor=vendor, published_at=published_at or timezone.now() - PAST
    )
    for language_code, values in translations.items():
        tool.create_translation(
            language_code,
            name=values.get("name", "Neutral tool"),
            short_description=values.get("short_description", ""),
            long_description=values.get("long_description", ""),
        )
    return tool


class ServiceIntegrationTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = make_author("service-integration-editor")

    def make_editorial(self, name, slug, *, title, texts=None):
        spec = EDITORIAL_SPECS[name]
        payload = {"title": title, "slug": slug}
        payload.update(texts or {})
        return publish(spec, author=self.author, translations={"en": payload})

    def search(self, term, language_code="en"):
        return search_site(raw_query=term, language_code=language_code)


@postgresql_only
class MixedResultTests(ServiceIntegrationTestCase):
    def test_one_object_of_every_kind_is_found(self):
        expected = {}
        for name in ("guide", "prompt", "usecase", "comparison"):
            obj = self.make_editorial(
                name, f"mixed-{name}-en", title="Mixedtoken heading"
            )
            expected[EDITORIAL_SPECS[name].kind] = obj.pk
        tool = make_tool(
            "mixed-tool", translations={"en": {"name": "Mixedtoken heading"}}
        )
        expected[SearchResultKind.TOOL] = tool.pk

        response = self.search("Mixedtoken")
        self.assertEqual(response.total_count, 5)
        self.assertEqual(
            {result.kind: result.object_id for result in response.results}, expected
        )
        for entry in response.counts:
            with self.subTest(kind=entry.kind):
                self.assertEqual(entry.count, 1)

    def test_selective_query_leaves_other_kinds_at_zero(self):
        self.make_editorial("guide", "sel-guide-en", title="Selectivetoken heading")
        make_tool("sel-tool", translations={"en": {"name": "Selectivetoken heading"}})

        response = self.search("Selectivetoken")
        self.assertEqual(response.total_count, 2)
        self.assertEqual(response.count_for(SearchResultKind.GUIDE), 1)
        self.assertEqual(response.count_for(SearchResultKind.TOOL), 1)
        for kind in (
            SearchResultKind.PROMPT,
            SearchResultKind.USE_CASE,
            SearchResultKind.COMPARISON,
        ):
            with self.subTest(kind=kind):
                self.assertEqual(response.count_for(kind), 0)

    def test_valid_query_without_matches_returns_an_empty_response(self):
        self.make_editorial("guide", "nomatch-guide-en", title="Something else")
        response = self.search("nothingmatchesthistoken")
        self.assertTrue(response.is_empty)
        self.assertEqual(response.total_count, 0)
        self.assertIsNone(response.query.issue)
        self.assertTrue(all(entry.count == 0 for entry in response.counts))


@postgresql_only
class LanguageIsolationTests(ServiceIntegrationTestCase):
    def test_service_returns_only_the_requested_language(self):
        for name in ("guide", "prompt", "usecase", "comparison"):
            spec = EDITORIAL_SPECS[name]
            publish(
                spec,
                author=self.author,
                translations={
                    "en": {"title": "Englishonly heading", "slug": f"lang-{name}-en"},
                    "de": {"title": "Deutschonly Titel", "slug": f"lang-{name}-de"},
                },
            )
        make_tool(
            "lang-tool",
            translations={
                "en": {"name": "Englishonly heading"},
                "de": {"name": "Deutschonly Titel"},
            },
        )

        english = self.search("Englishonly", "en")
        self.assertEqual(english.total_count, 5)
        self.assertTrue(all(r.language_code == "en" for r in english.results))
        self.assertEqual(self.search("Englishonly", "de").total_count, 0)

        german = self.search("Deutschonly", "de")
        self.assertEqual(german.total_count, 5)
        self.assertTrue(all(r.language_code == "de" for r in german.results))
        self.assertEqual(self.search("Deutschonly", "en").total_count, 0)

    def test_tool_parler_fallback_does_not_leak(self):
        make_tool(
            "fallback-tool", translations={"en": {"name": "Fallbacktoken Studio"}}
        )
        self.assertEqual(self.search("Fallbacktoken", "en").total_count, 1)
        self.assertEqual(self.search("Fallbacktoken", "de").total_count, 0)

    def test_editorial_draft_translation_does_not_leak(self):
        guide = self.make_editorial(
            "guide", "draft-guide-en", title="Publishedtoken heading"
        )
        edit_without_publishing(
            guide, language_code="en", title="Draftneedle heading"
        )
        self.assertEqual(self.search("Publishedtoken").total_count, 1)
        self.assertEqual(self.search("Draftneedle").total_count, 0)

    def test_ambient_language_does_not_influence_the_service(self):
        make_tool(
            "ambient-tool",
            translations={
                "en": {"name": "Englishonly heading"},
                "de": {"name": "Deutschonly Titel"},
            },
        )
        with translation.override("en"):
            response = self.search("Deutschonly", "de")
        self.assertEqual(response.total_count, 1)
        self.assertEqual(response.results[0].title, "Deutschonly Titel")
        self.assertTrue(response.results[0].url.startswith("/de/"))


@postgresql_only
class VisibilityTests(ServiceIntegrationTestCase):
    def test_future_tool_is_excluded(self):
        make_tool(
            "future-tool",
            published_at=timezone.now() + FUTURE,
            translations={"en": {"name": "Scheduledtoken Studio"}},
        )
        self.assertEqual(self.search("Scheduledtoken").total_count, 0)

    def test_draft_editorial_object_is_excluded(self):
        for name in ("guide", "prompt", "usecase", "comparison"):
            spec = EDITORIAL_SPECS[name]
            obj = spec.model.objects.create(
                status=EditorialWorkflowMixin.STATUS_DRAFT
            )
            obj.create_translation(
                "en",
                title="Draftstatustoken heading",
                slug=f"draft-status-{name}-en",
                **{field: "" for field in spec.text_fields},
                **spec.required_extra,
            )
        self.assertEqual(self.search("Draftstatustoken").total_count, 0)

    def test_review_semantics_stay_model_specific(self):
        # Guides and prompts keep a live revision public; use cases and
        # comparisons do not. The service reproduces whatever each adapter
        # decides - it has no visibility rule of its own.
        from search.tests.editorial_fixtures import begin_unpublished_revision

        expected_visible = set()
        for name in ("guide", "prompt", "usecase", "comparison"):
            spec = EDITORIAL_SPECS[name]
            obj = self.make_editorial(
                name, f"review-{name}-en", title="Reviewtoken heading"
            )
            begin_unpublished_revision(
                obj, author=self.author, language_code="en", intro="edited"
            )
            if spec.review_with_live_revision_is_public:
                expected_visible.add(spec.kind)

        response = self.search("Reviewtoken")
        self.assertEqual(
            {result.kind for result in response.results}, expected_visible
        )


@postgresql_only
class QueryCountTests(ServiceIntegrationTestCase):
    def test_one_query_per_adapter(self):
        for name in ("guide", "prompt", "usecase", "comparison"):
            self.make_editorial(name, f"count-{name}-en", title="Counttoken heading")
        make_tool("count-tool", translations={"en": {"name": "Counttoken heading"}})

        with CaptureQueriesContext(connection) as captured:
            response = self.search("Counttoken")
        self.assertEqual(response.total_count, 5)
        self.assertEqual(
            len(captured.captured_queries),
            len(SEARCH_ADAPTERS),
            "expected exactly one query per adapter and none for counts",
        )

    def test_legacy_records_add_no_extra_content_query(self):
        """
        A record predating the snapshot mechanism resolves its public values
        from the current translation, which parler reads through its own
        translation cache. This project configures Django's DatabaseCache, so
        each of those cache reads is itself a SQL statement - two per legacy
        object, against the cache table rather than against content.

        The number of content queries stays at one per adapter; only cache
        lookups are added, and a different cache backend would remove them
        entirely. Pinning both halves separately keeps a genuine content-level
        N+1 detectable.
        """
        from search.tests.editorial_fixtures import make_legacy

        legacy_count = 0
        for name in ("guide", "prompt", "usecase", "comparison"):
            make_legacy(
                EDITORIAL_SPECS[name],
                translations={
                    "en": {"title": "Legacytoken heading", "slug": f"legacy-{name}-en"}
                },
            )
            legacy_count += 1

        with CaptureQueriesContext(connection) as captured:
            response = self.search("Legacytoken")
        self.assertEqual(response.total_count, 4)

        cache_reads = [
            query
            for query in captured.captured_queries
            if "mentoroai_cache_table" in query["sql"]
        ]
        content_queries = len(captured.captured_queries) - len(cache_reads)
        self.assertEqual(
            content_queries,
            len(SEARCH_ADAPTERS),
            "content queries must stay at one per adapter",
        )
        self.assertEqual(len(cache_reads), 2 * legacy_count)

    def test_invalid_query_runs_no_query_at_all(self):
        for raw in ("", "a", "x" * 101, None):
            with self.subTest(raw=raw):
                with self.assertNumQueries(0):
                    search_site(raw_query=raw, language_code="en")


@postgresql_only
class ResultContractTests(ServiceIntegrationTestCase):
    def test_every_result_satisfies_the_public_contract(self):
        for name in ("guide", "prompt", "usecase", "comparison"):
            self.make_editorial(
                name,
                f"contract-{name}-en",
                title="Contracttoken heading",
                texts={"intro": "A readable <strong>intro</strong> text."},
            )
        make_tool(
            "contract-tool",
            translations={
                "en": {
                    "name": "Contracttoken heading",
                    "short_description": "A readable summary.",
                }
            },
        )

        response = self.search("Contracttoken")
        self.assertEqual(response.total_count, 5)
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        for result in response.results:
            with self.subTest(kind=result.kind):
                self.assertIsInstance(result, SearchResult)
                self.assertTrue(result.url.startswith("/en/"))
                self.assertNotIn("://", result.url)
                self.assertNotEqual(result.url, "#")
                self.assertEqual(result.language_code, "en")
                self.assertIs(type(result.summary), str)
                self.assertFalse(hasattr(result.summary, "__html__"))
                self.assertNotIn("<", result.summary)
                self.assertFalse(hasattr(result, "_meta"))
                self.assertEqual(self.client.get(result.url).status_code, 200)

    def test_response_carries_the_normalized_query_and_language(self):
        response = self.search("  Mixed   Token  ")
        self.assertEqual(response.query.value, "Mixed Token")
        self.assertIsNone(response.query.issue)
        self.assertEqual(response.language_code, "en")

    def test_invalid_query_reports_its_issue(self):
        self.assertIs(
            search_site(raw_query="a", language_code="en").query.issue,
            SearchQueryIssue.TOO_SHORT,
        )
