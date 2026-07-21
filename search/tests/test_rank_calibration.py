"""
Beta 10.7: are ranks from five very different content models comparable?

Tools carry an 8-character name; guides carry a 2000-character body. If those
produced systematically different scores for the same match, the mixed result
list would silently favour one content type - which is exactly what the
architecture forbids. These tests build deliberately identical content across
all five adapters and assert the ranks agree.

Measured, not assumed: no calibration factor exists, and none of these tests
would pass if one were introduced.

Requires PostgreSQL.
"""
from datetime import timedelta
from unittest import skipUnless

from django.db import connection
from django.test import TestCase
from django.utils import timezone

from catalog.models import Tool
from search.query import normalize_search_query
from search.ranking import (
    CONTENT_KIND_ORDER,
    RANK_PRECISION,
    SearchMatchTier,
    determine_match_tier,
)
from search.registry import SEARCH_ADAPTERS
from search.result_types import SearchMatchedField, SearchResultKind
from search.services import search_site
from search.tests.editorial_fixtures import ADAPTER_SPECS, make_author, publish

postgresql_only = skipUnless(
    connection.vendor == "postgresql", "PostgreSQL full-text search required"
)

#: Deliberately inert filler: no lexeme here collides with any search token.
NEUTRAL = "Nothing relevant in this sentence."
BODY_HIT = "<p>The bodyonlytoken appears once inside this paragraph.</p>"

EDITORIAL_NAMES = ("guide", "prompt", "usecase", "comparison")
EDITORIAL_SPECS = {spec.name: spec for spec in ADAPTER_SPECS}


class CalibrationTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = make_author("calibration-editor")
        cls.published_at = timezone.now() - timedelta(days=1)

    def make_editorial(self, name, slug, *, title, texts=None):
        spec = EDITORIAL_SPECS[name]
        payload = {"title": title, "slug": slug}
        payload.update(texts or {})
        obj = publish(spec, author=self.author, translations={"en": payload})
        # Identical publication dates keep recency out of the comparison.
        spec.model.objects.filter(pk=obj.pk).update(published_at=self.published_at)
        return obj

    def make_tool(self, slug, *, name, short="", long="", vendor=""):
        tool = Tool.objects.create(
            slug=slug, vendor=vendor, published_at=self.published_at
        )
        tool.create_translation(
            "en", name=name, short_description=short, long_description=long
        )
        return tool

    def build_identical(self, marker, *, title, texts, tool_short, tool_long):
        """One object per content type carrying deliberately identical text."""
        for name in EDITORIAL_NAMES:
            self.make_editorial(name, f"calib-{marker}-{name}", title=title, texts=texts)
        self.make_tool(f"calib-{marker}-tool", name=title, short=tool_short, long=tool_long)

    def response_for(self, term):
        return search_site(raw_query=term, language_code="en")

    def ranks_by_kind(self, term):
        return {
            result.kind: round(result.rank, RANK_PRECISION)
            for result in self.response_for(term).results
        }

    def tiers_by_kind(self, term):
        query = normalize_search_query(term)
        return {
            result.kind: determine_match_tier(result, query)
            for result in self.response_for(term).results
        }


@postgresql_only
class IdenticalContentRanksEquallyTests(CalibrationTestCase):
    def test_identical_exact_title(self):
        self.build_identical(
            "exact",
            title="Calibrationtoken",
            texts={"intro": NEUTRAL, "body": NEUTRAL, "outro": NEUTRAL},
            tool_short=NEUTRAL,
            tool_long=NEUTRAL,
        )
        tiers = self.tiers_by_kind("Calibrationtoken")
        ranks = self.ranks_by_kind("Calibrationtoken")
        self.assertEqual(len(ranks), 5)
        self.assertTrue(
            all(tier is SearchMatchTier.TITLE_EXACT for tier in tiers.values())
        )
        self.assertEqual(
            len(set(ranks.values())), 1, f"ranks differ across content types: {ranks}"
        )

    def test_identical_title_prefix(self):
        self.build_identical(
            "prefix",
            title="Prefixtoken with a trailing remainder",
            texts={"intro": NEUTRAL, "body": NEUTRAL, "outro": NEUTRAL},
            tool_short=NEUTRAL,
            tool_long=NEUTRAL,
        )
        tiers = self.tiers_by_kind("Prefixtoken")
        ranks = self.ranks_by_kind("Prefixtoken")
        self.assertEqual(len(ranks), 5)
        self.assertTrue(
            all(tier is SearchMatchTier.TITLE_PREFIX for tier in tiers.values())
        )
        self.assertEqual(len(set(ranks.values())), 1, f"ranks differ: {ranks}")

    def test_identical_title_contains(self):
        self.build_identical(
            "contains",
            title="A leading part Containstoken and more",
            texts={"intro": NEUTRAL, "body": NEUTRAL, "outro": NEUTRAL},
            tool_short=NEUTRAL,
            tool_long=NEUTRAL,
        )
        tiers = self.tiers_by_kind("Containstoken")
        ranks = self.ranks_by_kind("Containstoken")
        self.assertEqual(len(ranks), 5)
        self.assertTrue(
            all(tier is SearchMatchTier.TITLE_CONTAINS for tier in tiers.values())
        )
        self.assertEqual(len(set(ranks.values())), 1, f"ranks differ: {ranks}")

    def test_identical_body_only_hit(self):
        self.build_identical(
            "body",
            title="Unrelated heading here",
            texts={"intro": NEUTRAL, "body": BODY_HIT, "outro": ""},
            tool_short=NEUTRAL,
            tool_long=BODY_HIT,
        )
        tiers = self.tiers_by_kind("bodyonlytoken")
        ranks = self.ranks_by_kind("bodyonlytoken")
        self.assertEqual(len(ranks), 5)
        self.assertTrue(
            all(tier is SearchMatchTier.FULL_TEXT for tier in tiers.values())
        )
        self.assertEqual(len(set(ranks.values())), 1, f"ranks differ: {ranks}")

    def test_three_field_and_four_field_adapters_rank_alike(self):
        # Prompts and use cases index an outro that guides, comparisons and
        # tools do not. An empty one must add no rank mass.
        self.build_identical(
            "fields",
            title="Unrelated heading here",
            texts={"intro": NEUTRAL, "body": BODY_HIT, "outro": ""},
            tool_short=NEUTRAL,
            tool_long=BODY_HIT,
        )
        ranks = self.ranks_by_kind("bodyonlytoken")
        four_field = {ranks[SearchResultKind.PROMPT], ranks[SearchResultKind.USE_CASE]}
        three_field = {
            ranks[SearchResultKind.GUIDE],
            ranks[SearchResultKind.COMPARISON],
            ranks[SearchResultKind.TOOL],
        }
        self.assertEqual(four_field, three_field)

    def test_empty_tool_metadata_adds_no_rank(self):
        tool = self.make_tool(
            "calib-meta-empty",
            name="Unrelated heading here",
            short=NEUTRAL,
            long=BODY_HIT,
            vendor="",
        )
        self.make_editorial(
            "guide",
            "calib-meta-guide",
            title="Unrelated heading here",
            texts={"intro": NEUTRAL, "body": BODY_HIT},
        )
        ranks = self.ranks_by_kind("bodyonlytoken")
        self.assertEqual(tool.vendor, "")
        self.assertEqual(ranks[SearchResultKind.TOOL], ranks[SearchResultKind.GUIDE])

    def test_ordering_of_identical_content_falls_to_the_kind_tie_break(self):
        self.build_identical(
            "tiebreak",
            title="Calibrationtoken",
            texts={"intro": NEUTRAL, "body": NEUTRAL, "outro": NEUTRAL},
            tool_short=NEUTRAL,
            tool_long=NEUTRAL,
        )
        kinds = [result.kind for result in self.response_for("Calibrationtoken").results]
        self.assertEqual(kinds, list(CONTENT_KIND_ORDER))


@postgresql_only
class RealAdditionalMatchesRaiseRankTests(CalibrationTestCase):
    def test_a_second_matching_field_raises_the_rank(self):
        # Not a type bonus: the prompt genuinely contains the term twice.
        self.make_editorial(
            "prompt",
            "multi-prompt",
            title="Unrelated heading here",
            texts={
                "intro": NEUTRAL,
                "body": "<p>The multitoken appears here.</p>",
                "outro": "The multitoken appears again.",
            },
        )
        self.make_editorial(
            "guide",
            "multi-guide",
            title="Unrelated heading here",
            texts={"intro": NEUTRAL, "body": "<p>The multitoken appears here.</p>"},
        )
        ranks = self.ranks_by_kind("multitoken")
        self.assertGreater(ranks[SearchResultKind.PROMPT], ranks[SearchResultKind.GUIDE])

    def test_tool_vendor_match_raises_the_rank(self):
        self.make_tool(
            "vendor-both",
            name="Vendortoken Studio",
            short=NEUTRAL,
            long=NEUTRAL,
            vendor="Vendortoken Inc",
        )
        self.make_tool(
            "vendor-name-only", name="Vendortoken Studio", short=NEUTRAL, long=NEUTRAL
        )
        results = self.response_for("Vendortoken").results
        self.assertEqual(len(results), 2)
        self.assertGreater(
            round(results[0].rank, RANK_PRECISION), round(results[1].rank, RANK_PRECISION)
        )


@postgresql_only
class PrecedenceTests(CalibrationTestCase):
    def test_match_tier_beats_rank(self):
        exact = self.make_editorial(
            "guide", "prec-exact", title="Precedencetoken", texts={"intro": NEUTRAL}
        )
        strong_body = self.make_tool(
            "prec-body",
            name="Unrelated heading here",
            short=NEUTRAL,
            long="<p>" + ("Precedencetoken " * 40) + "</p>",
        )
        order = [
            (result.kind, result.object_id)
            for result in self.response_for("Precedencetoken").results
        ]
        self.assertEqual(
            order,
            [(SearchResultKind.GUIDE, exact.pk), (SearchResultKind.TOOL, strong_body.pk)],
        )

    def test_rank_beats_recency_within_one_tier(self):
        strong_old = self.make_editorial(
            "guide",
            "prec-strong-old",
            title="Unrelated heading here",
            texts={"intro": NEUTRAL, "body": "<p>" + ("Recencytoken " * 30) + "</p>"},
        )
        weak_new = self.make_tool(
            "prec-weak-new",
            name="Unrelated heading here",
            short=NEUTRAL,
            long="<p>" + ("filler " * 200) + "Recencytoken</p>",
        )
        Tool.objects.filter(pk=weak_new.pk).update(published_at=timezone.now())
        order = [
            (result.kind, result.object_id)
            for result in self.response_for("Recencytoken").results
        ]
        self.assertEqual(order[0], (SearchResultKind.GUIDE, strong_old.pk))

    def test_metadata_beats_a_body_hit_but_loses_to_a_title_hit(self):
        self.make_tool(
            "prec-meta",
            name="Unrelated heading here",
            short=NEUTRAL,
            long=NEUTRAL,
            vendor="Tiertoken Inc",
        )
        self.make_editorial(
            "guide",
            "prec-meta-body",
            title="Unrelated heading here",
            texts={"intro": NEUTRAL, "body": "<p>The Tiertoken sits in the body.</p>"},
        )
        self.make_editorial(
            "prompt", "prec-meta-title", title="Tiertoken heading", texts={"intro": NEUTRAL}
        )

        query = normalize_search_query("Tiertoken")
        results = self.response_for("Tiertoken").results
        tiers = {result.kind: determine_match_tier(result, query) for result in results}
        matched = {result.kind: result.matched_field for result in results}

        self.assertIs(matched[SearchResultKind.TOOL], SearchMatchedField.METADATA)
        self.assertIs(tiers[SearchResultKind.TOOL], SearchMatchTier.METADATA)
        self.assertIs(tiers[SearchResultKind.GUIDE], SearchMatchTier.FULL_TEXT)
        self.assertEqual(
            [result.kind for result in results],
            [SearchResultKind.PROMPT, SearchResultKind.TOOL, SearchResultKind.GUIDE],
        )


@postgresql_only
class RegistryPermutationTests(CalibrationTestCase):
    def _order_and_counts(self, adapters):
        response = search_site(
            raw_query="Calibrationtoken", language_code="en", adapters=adapters
        )
        return [(r.kind, r.object_id) for r in response.results], response.counts

    def test_registry_order_does_not_influence_results(self):
        self.build_identical(
            "perm",
            title="Calibrationtoken",
            texts={"intro": NEUTRAL, "body": NEUTRAL, "outro": NEUTRAL},
            tool_short=NEUTRAL,
            tool_long=NEUTRAL,
        )
        baseline_order, baseline_counts = self._order_and_counts(SEARCH_ADAPTERS)
        self.assertEqual(len(baseline_order), 5)

        for label, permutation in (
            ("reversed", tuple(reversed(SEARCH_ADAPTERS))),
            ("rotated", SEARCH_ADAPTERS[2:] + SEARCH_ADAPTERS[:2]),
            ("swapped", (SEARCH_ADAPTERS[4],) + SEARCH_ADAPTERS[1:4] + (SEARCH_ADAPTERS[0],)),
        ):
            with self.subTest(permutation=label):
                order, counts = self._order_and_counts(permutation)
                self.assertEqual(order, baseline_order)
                self.assertEqual(counts, baseline_counts)


@postgresql_only
class NoAdapterLocalNormalizationTests(CalibrationTestCase):
    def test_a_lone_weak_hit_is_not_scaled_up(self):
        # The comparison adapter returns a single weak hit. If it rescaled
        # against its own maximum it would become 1.0 and lead the list.
        strong = self.make_editorial(
            "guide", "norm-strong", title="Normalizationtoken", texts={"intro": NEUTRAL}
        )
        weak = self.make_editorial(
            "comparison",
            "norm-weak",
            title="Unrelated heading here",
            texts={
                "intro": NEUTRAL,
                "body": "<p>" + ("filler " * 200) + "Normalizationtoken</p>",
            },
        )
        results = self.response_for("Normalizationtoken").results
        self.assertEqual(
            [(r.kind, r.object_id) for r in results],
            [
                (SearchResultKind.GUIDE, strong.pk),
                (SearchResultKind.COMPARISON, weak.pk),
            ],
        )
        weak_rank = next(
            r.rank for r in results if r.kind is SearchResultKind.COMPARISON
        )
        self.assertLess(weak_rank, 0.5)
