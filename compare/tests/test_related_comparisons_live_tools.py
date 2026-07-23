"""
Beta 11.9E: related_comparisons() ranks source and candidates by their
*published* tool membership, not the current ComparisonToolEntry draft
rows.

Before this slice, ``core/services.py::related_comparisons()`` computed
``tool_ids``/``cat_ids`` from ``comparison.tools.all()`` (the source) and
matched candidates via ``Q(tools__in=tool_ids) | Q(tools__categories__in=...)``
(the candidates) - both the current draft M2M through
``ComparisonToolEntry``. A draft tool swap, a new draft entry, or a draft
deletion on either side of the match could therefore change which
comparisons were selected as "related" and in what order, before the
change was ever republished - even though the detail page (Beta 11.9), the
category filter (Beta 11.9C) and the list cards (Beta 11.9D) already stayed
on the last published snapshot.

This module pins the fix: both sides of the match now read
``compare.presentation.live_tool_ids_for_comparisons()`` - the same
State-A/State-C boundary and ``Tool.objects.public()`` contract every other
public Comparison surface already uses.
"""
from datetime import timedelta

from django.conf import settings
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone, translation

from catalog.models import Category, Tool
from compare.models import Comparison
from core.services import related_comparisons, to_teaser_item
from compare.tests.live_snapshot_fixtures import (
    add_entry,
    make_comparison,
    make_tool,
    make_user,
    publish,
    start_review_round,
)

PAST = timezone.now() - timedelta(days=1)
FUTURE = timezone.now() + timedelta(days=30)


def make_category(slug, name=None, language="en"):
    cat = Category.objects.create()
    cat.create_translation(language, name=name or slug, slug=slug)
    return cat


class RelatedLiveToolsTestCase(TestCase):
    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.author = make_user("cmp-relatedlive-author")

    def _rel(self, source, limit=6, language_code="en"):
        return related_comparisons(
            Comparison.objects.get(pk=source.pk), limit=limit, language_code=language_code
        )

    def _pks(self, source, **kwargs):
        return [c.pk for c in self._rel(source, **kwargs)]


class SourceDraftToolSwapTests(RelatedLiveToolsTestCase):
    """Group A: the primary reproduction scenario."""

    def setUp(self):
        super().setUp()
        self.tool_a = make_tool("relsrc-tool-a", "RelSrc Tool A", published_at=PAST)
        self.tool_b = make_tool("relsrc-tool-b", "RelSrc Tool B", published_at=PAST)

        self.source = make_comparison(slug="relsrc-source", title="RelSrc Source", author=self.author)
        self.source_entry = add_entry(self.source, self.tool_a, position=10, summary="S")
        self.source = publish(self.source, self.author)

        self.cand_a = make_comparison(slug="relsrc-cand-a", title="RelSrc Candidate A", author=self.author)
        add_entry(self.cand_a, self.tool_a, position=10, summary="S")
        self.cand_a = publish(self.cand_a, self.author)

        self.cand_b = make_comparison(slug="relsrc-cand-b", title="RelSrc Candidate B", author=self.author)
        add_entry(self.cand_b, self.tool_b, position=10, summary="S")
        self.cand_b = publish(self.cand_b, self.author)

    def test_candidate_a_ranks_first_while_source_is_live_a(self):
        pks = self._pks(self.source)
        self.assertIn(self.cand_a.pk, pks)
        if self.cand_b.pk in pks:
            self.assertLess(pks.index(self.cand_a.pk), pks.index(self.cand_b.pk))

    def test_ranking_stays_a_based_through_review_after_draft_swap(self):
        reviewed = start_review_round(self.source, self.author)
        fresh_entry = reviewed.tool_entries.get(pk=self.source_entry.pk)
        fresh_entry.tool = self.tool_b
        fresh_entry.save(update_fields=["tool"])

        pks = self._pks(self.source)
        self.assertIn(self.cand_a.pk, pks)
        if self.cand_b.pk in pks:
            self.assertLess(pks.index(self.cand_a.pk), pks.index(self.cand_b.pk))

    def test_republish_switches_ranking_to_b_based(self):
        reviewed = start_review_round(self.source, self.author)
        fresh_entry = reviewed.tool_entries.get(pk=self.source_entry.pk)
        fresh_entry.tool = self.tool_b
        fresh_entry.save(update_fields=["tool"])
        publish(Comparison.objects.get(pk=self.source.pk), self.author)

        pks = self._pks(self.source)
        self.assertIn(self.cand_b.pk, pks)
        if self.cand_a.pk in pks:
            self.assertLess(pks.index(self.cand_b.pk), pks.index(self.cand_a.pk))


class CandidateDraftToolSwapTests(RelatedLiveToolsTestCase):
    """Group B."""

    def setUp(self):
        super().setUp()
        self.tool_a = make_tool("relcand-tool-a", "RelCand Tool A", published_at=PAST)
        self.tool_b = make_tool("relcand-tool-b", "RelCand Tool B", published_at=PAST)

        self.source = make_comparison(slug="relcand-source", title="RelCand Source", author=self.author)
        add_entry(self.source, self.tool_a, position=10, summary="S")
        self.source = publish(self.source, self.author)

        self.candidate = make_comparison(slug="relcand-candidate", title="RelCand Candidate", author=self.author)
        self.cand_entry = add_entry(self.candidate, self.tool_a, position=10, summary="S")
        self.candidate = publish(self.candidate, self.author)

        # A permanently tool-A-matched rival: with it present, an unmatched
        # candidate can only win a slot through the unchanged "always
        # return something useful" fallback-fill, never through match
        # ranking - so a bare presence/absence check at limit=1 would be
        # unreliable (the fallback alone can still surface a lone
        # candidate). Comparing against the rival's rank isolates the
        # actual match contribution.
        self.rival = make_comparison(slug="relcand-rival", title="RelCand Rival", author=self.author)
        add_entry(self.rival, self.tool_a, position=10, summary="S")
        self.rival = publish(self.rival, self.author)

    def test_match_remains_before_republish(self):
        self.assertIn(self.candidate.pk, self._pks(self.source))

        reviewed = start_review_round(self.candidate, self.author)
        fresh_entry = reviewed.tool_entries.get(pk=self.cand_entry.pk)
        fresh_entry.tool = self.tool_b
        fresh_entry.save(update_fields=["tool"])

        self.assertIn(self.candidate.pk, self._pks(self.source))

    def test_match_changes_after_republish(self):
        reviewed = start_review_round(self.candidate, self.author)
        fresh_entry = reviewed.tool_entries.get(pk=self.cand_entry.pk)
        fresh_entry.tool = self.tool_b
        fresh_entry.save(update_fields=["tool"])
        publish(Comparison.objects.get(pk=self.candidate.pk), self.author)

        # No longer tool-A-matched: the still-matched rival must outrank it.
        pks = self._pks(self.source, limit=2)
        self.assertIn(self.rival.pk, pks)
        self.assertEqual(pks[0], self.rival.pk)


class NewCandidateDraftEntryTests(RelatedLiveToolsTestCase):
    """Group C."""

    def setUp(self):
        super().setUp()
        self.tool_a = make_tool("relnew-tool-a", "RelNew Tool A", published_at=PAST)

        self.source = make_comparison(slug="relnew-source", title="RelNew Source", author=self.author)
        add_entry(self.source, self.tool_a, position=10, summary="S")
        self.source = publish(self.source, self.author)

        self.candidate = make_comparison(slug="relnew-candidate", title="RelNew Candidate", author=self.author)
        self.candidate = publish(self.candidate, self.author)  # no tools at all

        self.rival = make_comparison(slug="relnew-rival", title="RelNew Rival", author=self.author)
        add_entry(self.rival, self.tool_a, position=10, summary="S")
        self.rival = publish(self.rival, self.author)

    def test_no_match_bonus_before_republish(self):
        reviewed = start_review_round(self.candidate, self.author)
        add_entry(reviewed, self.tool_a, position=10, summary="S")

        # The draft-only match must not outrank the genuinely matched rival.
        pks = self._pks(self.source, limit=2)
        self.assertIn(self.rival.pk, pks)
        self.assertEqual(pks[0], self.rival.pk)

    def test_match_bonus_after_republish(self):
        reviewed = start_review_round(self.candidate, self.author)
        add_entry(reviewed, self.tool_a, position=10, summary="S")
        publish(Comparison.objects.get(pk=self.candidate.pk), self.author)

        pks = self._pks(self.source)
        self.assertIn(self.candidate.pk, pks)


class CandidateDraftDeletionTests(RelatedLiveToolsTestCase):
    """Group D."""

    def setUp(self):
        super().setUp()
        self.tool_a = make_tool("reldel-tool-a", "RelDel Tool A", published_at=PAST)

        self.source = make_comparison(slug="reldel-source", title="RelDel Source", author=self.author)
        add_entry(self.source, self.tool_a, position=10, summary="S")
        self.source = publish(self.source, self.author)

        self.candidate = make_comparison(slug="reldel-candidate", title="RelDel Candidate", author=self.author)
        self.cand_entry = add_entry(self.candidate, self.tool_a, position=10, summary="S")
        self.candidate = publish(self.candidate, self.author)

        self.rival = make_comparison(slug="reldel-rival", title="RelDel Rival", author=self.author)
        add_entry(self.rival, self.tool_a, position=10, summary="S")
        self.rival = publish(self.rival, self.author)

    def test_match_persists_before_republish(self):
        reviewed = start_review_round(self.candidate, self.author)
        reviewed.tool_entries.get(pk=self.cand_entry.pk).delete()

        self.assertIn(self.candidate.pk, self._pks(self.source))

    def test_match_removed_after_republish(self):
        reviewed = start_review_round(self.candidate, self.author)
        reviewed.tool_entries.get(pk=self.cand_entry.pk).delete()
        publish(Comparison.objects.get(pk=self.candidate.pk), self.author)

        pks = self._pks(self.source, limit=2)
        self.assertIn(self.rival.pk, pks)
        self.assertEqual(pks[0], self.rival.pk)


class SourceNewDraftEntryTests(RelatedLiveToolsTestCase):
    """Group E."""

    def setUp(self):
        super().setUp()
        self.tool_a = make_tool("relsrcnew-tool-a", "RelSrcNew Tool A", published_at=PAST)
        self.tool_b = make_tool("relsrcnew-tool-b", "RelSrcNew Tool B", published_at=PAST)

        self.source = make_comparison(slug="relsrcnew-source", title="RelSrcNew Source", author=self.author)
        add_entry(self.source, self.tool_a, position=10, summary="S")
        self.source = publish(self.source, self.author)

        self.cand_b = make_comparison(slug="relsrcnew-cand-b", title="RelSrcNew Candidate B", author=self.author)
        add_entry(self.cand_b, self.tool_b, position=10, summary="S")
        self.cand_b = publish(self.cand_b, self.author)

        self.rival = make_comparison(slug="relsrcnew-rival", title="RelSrcNew Rival", author=self.author)
        add_entry(self.rival, self.tool_a, position=10, summary="S")
        self.rival = publish(self.rival, self.author)

    def test_b_candidate_gets_no_bonus_before_republish(self):
        reviewed = start_review_round(self.source, self.author)
        add_entry(reviewed, self.tool_b, position=20, summary="S")

        # cand_b's only match is the draft-only tool B; the rival's live
        # tool-A match must still outrank it.
        pks = self._pks(self.source, limit=2)
        self.assertIn(self.rival.pk, pks)
        self.assertEqual(pks[0], self.rival.pk)

    def test_b_candidate_gets_bonus_after_republish(self):
        reviewed = start_review_round(self.source, self.author)
        add_entry(reviewed, self.tool_b, position=20, summary="S")
        publish(Comparison.objects.get(pk=self.source.pk), self.author)

        self.assertIn(self.cand_b.pk, self._pks(self.source))


class GlobalToolStatusTests(RelatedLiveToolsTestCase):
    """Group F.

    ``tool_a`` is the tool whose global visibility gets toggled; ``tool_c``
    is a permanently-public second source tool, present only so the source
    always has *some* live match (through the rival, via tool_c) to compare
    against - otherwise, with tool_a not public and no other match, the
    function's unchanged "always return something useful" fallback would
    surface the lone candidate regardless of tool_a's state, making a bare
    presence check unreliable (see the sibling test classes above for the
    same fix).
    """

    def setUp(self):
        super().setUp()
        self.tool_a = make_tool("relglobal-tool-a", "RelGlobal Tool A", published_at=FUTURE)
        self.tool_c = make_tool("relglobal-tool-c", "RelGlobal Tool C", published_at=PAST)

        self.source = make_comparison(slug="relglobal-source", title="RelGlobal Source", author=self.author)
        add_entry(self.source, self.tool_a, position=10, summary="S")
        add_entry(self.source, self.tool_c, position=20, summary="S")
        self.source = publish(self.source, self.author)

        self.candidate = make_comparison(slug="relglobal-candidate", title="RelGlobal Candidate", author=self.author)
        add_entry(self.candidate, self.tool_a, position=10, summary="S")
        self.candidate = publish(self.candidate, self.author)

        self.rival = make_comparison(slug="relglobal-rival", title="RelGlobal Rival", author=self.author)
        add_entry(self.rival, self.tool_c, position=10, summary="S")
        self.rival = publish(self.rival, self.author)

    def test_no_match_while_tool_not_public(self):
        pks = self._pks(self.source, limit=2)
        self.assertIn(self.rival.pk, pks)
        self.assertEqual(pks[0], self.rival.pk)

    def test_match_once_tool_becomes_public_no_republish(self):
        Tool.objects.filter(pk=self.tool_a.pk).update(published_at=PAST)
        self.assertIn(self.candidate.pk, self._pks(self.source))

    def test_match_removed_once_tool_withdrawn_again(self):
        Tool.objects.filter(pk=self.tool_a.pk).update(published_at=PAST)
        Tool.objects.filter(pk=self.tool_a.pk).update(published_at=FUTURE)
        pks = self._pks(self.source, limit=2)
        self.assertIn(self.rival.pk, pks)
        self.assertEqual(pks[0], self.rival.pk)

    def test_deleted_tool_no_error_no_match(self):
        Tool.objects.filter(pk=self.tool_a.pk).update(published_at=PAST)
        self.tool_a.delete()
        rel = self._rel(self.source, limit=2)
        self.assertIsInstance(rel, list)
        pks = [c.pk for c in rel]
        self.assertIn(self.rival.pk, pks)
        self.assertEqual(pks[0], self.rival.pk)


class SnapshotMembershipTests(RelatedLiveToolsTestCase):
    """Group G."""

    def setUp(self):
        super().setUp()
        self.tool_a = make_tool("relmemb-tool-a", "RelMemb Tool A", published_at=PAST)

        self.source = make_comparison(slug="relmemb-source", title="RelMemb Source", author=self.author)
        add_entry(self.source, self.tool_a, position=10, summary="S")
        self.source = publish(self.source, self.author)

        self.rival = make_comparison(slug="relmemb-rival", title="RelMemb Rival", author=self.author)
        add_entry(self.rival, self.tool_a, position=10, summary="S")
        self.rival = publish(self.rival, self.author)

    def test_draft_only_candidate_tool_has_no_influence(self):
        candidate = make_comparison(slug="relmemb-candidate", title="RelMemb Candidate", author=self.author)
        candidate = publish(candidate, self.author)
        reviewed = start_review_round(candidate, self.author)
        add_entry(reviewed, self.tool_a, position=10, summary="S")

        pks = self._pks(self.source, limit=2)
        self.assertIn(self.rival.pk, pks)
        self.assertEqual(pks[0], self.rival.pk)

    def test_snapshot_member_candidate_has_influence(self):
        candidate2 = make_comparison(slug="relmemb-candidate2", title="RelMemb Candidate2", author=self.author)
        add_entry(candidate2, self.tool_a, position=10, summary="S")
        candidate2 = publish(candidate2, self.author)

        self.assertIn(candidate2.pk, self._pks(self.source))


class StateCTests(RelatedLiveToolsTestCase):
    """Group H."""

    def setUp(self):
        super().setUp()
        self.tool_a = make_tool("relstatec-tool-a", "RelStateC Tool A", published_at=PAST)

        self.source = make_comparison(slug="relstatec-source", title="RelStateC Source", author=self.author)
        add_entry(self.source, self.tool_a, position=10, summary="S")
        self.source = publish(self.source, self.author)

        self.legacy = Comparison.objects.create(status="published", published_at=timezone.now())
        self.legacy.create_translation(
            "en", title="RelStateC Legacy", intro="i", body="b", slug="relstatec-legacy"
        )
        self.legacy.tool_entries.create(tool=self.tool_a, position=10).create_translation(
            "en", label="", summary="Legacy summary", pros="", cons="", special=""
        )

        self.rival = make_comparison(slug="relstatec-rival", title="RelStateC Rival", author=self.author)
        add_entry(self.rival, self.tool_a, position=10, summary="S")
        self.rival = publish(self.rival, self.author)

    def test_legacy_published_record_is_matched(self):
        self.assertIsNone(self.legacy.live_entries)
        self.assertIn(self.legacy.pk, self._pks(self.source))

    def test_legacy_record_in_review_is_excluded(self):
        fresh = Comparison.objects.get(pk=self.legacy.pk)
        fresh.move_to_review(by=self.author)
        fresh.save()
        self.assertNotIn(self.legacy.pk, self._pks(self.source))

    def test_empty_snapshot_produces_no_match(self):
        Comparison.objects.filter(pk=self.legacy.pk).update(live_entries=[])
        pks = self._pks(self.source, limit=2)
        self.assertIn(self.rival.pk, pks)
        self.assertEqual(pks[0], self.rival.pk)


class LanguageTests(RelatedLiveToolsTestCase):
    """Group I."""

    def setUp(self):
        super().setUp()
        self.tool_a = make_tool("rellang-tool-a", "RelLang Tool A", published_at=PAST)

        self.source = make_comparison(slug="rellang-source-en", title="RelLang Source EN", author=self.author)
        add_entry(self.source, self.tool_a, position=10, summary="S")
        self.source = publish(self.source, self.author)

        self.en_candidate = make_comparison(slug="rellang-cand-en", title="RelLang Candidate EN", author=self.author)
        add_entry(self.en_candidate, self.tool_a, position=10, summary="S")
        self.en_candidate = publish(self.en_candidate, self.author)

    def test_en_candidate_appears_only_in_en(self):
        self.assertIn(self.en_candidate.pk, self._pks(self.source, language_code="en"))

    def test_de_translation_only_draft_does_not_activate_de_candidate(self):
        reviewed = start_review_round(self.en_candidate, self.author)
        reviewed.create_translation(
            "de", title="RelLang Candidate DE Draft", intro="i", body="b", slug="rellang-cand-de-draft"
        )
        de_source = make_comparison(slug="rellang-source-de", title="RelLang Source DE", author=self.author)
        de_source.create_translation(
            "de", title="RelLang Source DE", intro="i", body="b", slug="rellang-source-de-2"
        )
        add_entry(de_source, self.tool_a, position=10, summary="S")
        de_source = publish(de_source, self.author)

        pks_de = self._pks(de_source, language_code="de")
        self.assertNotIn(self.en_candidate.pk, pks_de)

    def test_related_card_title_and_url_are_live(self):
        rel = self._rel(self.source, language_code="en")
        item = to_teaser_item(
            next(c for c in rel if c.pk == self.en_candidate.pk), "comparison", language_code="en"
        )
        self.assertEqual(item["title"], "RelLang Candidate EN")
        self.assertIn("rellang-cand-en", item["url"])


class RankingFactorsUnchangedTests(RelatedLiveToolsTestCase):
    """Group J: existing weight/tie-breaker/limit/exclusion/fallback
    behaviour, re-verified against the new tool-ID source."""

    def test_tool_match_outranks_category_only_match(self):
        cat = make_category("reljfactor-cat", "RelJFactor Cat")
        tool_shared = make_tool("reljfactor-tool-shared", "RelJFactor Tool Shared", published_at=PAST)
        tool_cat_only = make_tool(
            "reljfactor-tool-catonly", "RelJFactor Tool CatOnly", published_at=PAST
        )
        tool_cat_only.categories.set([cat])
        tool_shared.categories.set([cat])

        source = make_comparison(slug="reljfactor-source", title="RelJFactor Source", author=self.author)
        add_entry(source, tool_shared, position=10, summary="S")
        source = publish(source, self.author)

        tool_match_cand = make_comparison(slug="reljfactor-toolmatch", title="RelJFactor ToolMatch", author=self.author)
        add_entry(tool_match_cand, tool_shared, position=10, summary="S")
        tool_match_cand = publish(tool_match_cand, self.author)

        cat_only_cand = make_comparison(slug="reljfactor-catonly", title="RelJFactor CatOnly", author=self.author)
        add_entry(cat_only_cand, tool_cat_only, position=10, summary="S")
        cat_only_cand = publish(cat_only_cand, self.author)

        pks = self._pks(source)
        self.assertIn(tool_match_cand.pk, pks)
        self.assertIn(cat_only_cand.pk, pks)
        self.assertLess(pks.index(tool_match_cand.pk), pks.index(cat_only_cand.pk))

    def test_source_is_excluded_from_its_own_results(self):
        tool = make_tool("reljexclude-tool", "RelJExclude Tool", published_at=PAST)
        source = make_comparison(slug="reljexclude-source", title="RelJExclude Source", author=self.author)
        add_entry(source, tool, position=10, summary="S")
        source = publish(source, self.author)
        self.assertNotIn(source.pk, self._pks(source))

    def test_limit_is_respected(self):
        tool = make_tool("reljlimit-tool", "RelJLimit Tool", published_at=PAST)
        source = make_comparison(slug="reljlimit-source", title="RelJLimit Source", author=self.author)
        add_entry(source, tool, position=10, summary="S")
        source = publish(source, self.author)
        for i in range(5):
            c = make_comparison(slug=f"reljlimit-cand-{i}", title=f"RelJLimit Cand {i}", author=self.author)
            add_entry(c, tool, position=10, summary="S")
            publish(c, self.author)
        self.assertEqual(len(self._pks(source, limit=3)), 3)

    def test_fallback_fill_still_works_with_no_tools(self):
        source = make_comparison(slug="reljfallback-source", title="RelJFallback Source", author=self.author)
        source = publish(source, self.author)
        filler = make_comparison(slug="reljfallback-filler", title="RelJFallback Filler", author=self.author)
        filler = publish(filler, self.author)
        pks = self._pks(source, limit=3)
        self.assertIn(filler.pk, pks)

    def test_no_duplicate_candidates(self):
        tool_1 = make_tool("reljdupe-tool-1", "RelJDupe Tool 1", published_at=PAST)
        tool_2 = make_tool("reljdupe-tool-2", "RelJDupe Tool 2", published_at=PAST)
        source = make_comparison(slug="reljdupe-source", title="RelJDupe Source", author=self.author)
        add_entry(source, tool_1, position=10, summary="S")
        add_entry(source, tool_2, position=20, summary="S")
        source = publish(source, self.author)

        candidate = make_comparison(slug="reljdupe-candidate", title="RelJDupe Candidate", author=self.author)
        add_entry(candidate, tool_1, position=10, summary="S")
        add_entry(candidate, tool_2, position=20, summary="S")
        candidate = publish(candidate, self.author)

        pks = self._pks(source)
        self.assertEqual(pks.count(candidate.pk), 1)


class SurfaceIntegrationTests(RelatedLiveToolsTestCase):
    """Group K."""

    def setUp(self):
        super().setUp()
        self.tool_a = make_tool("relsurf-tool-a", "RelSurf Tool A", published_at=PAST)
        self.tool_b = make_tool("relsurf-tool-b", "RelSurf Tool B", published_at=PAST)

        self.source = make_comparison(slug="relsurf-source", title="RelSurf Source", author=self.author)
        self.source_entry = add_entry(self.source, self.tool_a, position=10, summary="S")
        self.source = publish(self.source, self.author)

        self.cand_a = make_comparison(slug="relsurf-cand-a", title="RelSurf Candidate A", author=self.author)
        add_entry(self.cand_a, self.tool_a, position=10, summary="S")
        self.cand_a = publish(self.cand_a, self.author)

    def test_detail_page_related_cards_match_service_result(self):
        html = self.client.get("/en/compare/relsurf-source/").content.decode()
        self.assertIn("RelSurf Candidate A", html)

    def test_draft_swap_does_not_change_detail_page_cards_before_republish(self):
        reviewed = start_review_round(self.source, self.author)
        fresh_entry = reviewed.tool_entries.get(pk=self.source_entry.pk)
        fresh_entry.tool = self.tool_b
        fresh_entry.save(update_fields=["tool"])

        html = self.client.get("/en/compare/relsurf-source/").content.decode()
        self.assertIn("RelSurf Candidate A", html)

    def test_no_admin_or_preview_link_in_related_cards(self):
        html = self.client.get("/en/compare/relsurf-source/").content.decode()
        self.assertNotIn("/admin/", html)
        self.assertNotIn("/preview/", html)


class QueryCountTests(RelatedLiveToolsTestCase):
    """Group L."""

    def test_query_count_does_not_scale_with_candidate_count(self):
        # Both pools stay >= limit so the (unchanged, itself query-bearing)
        # fallback-fill never triggers in either case - otherwise a smaller
        # pool would legitimately cost one extra query than a larger one,
        # which would be the fallback contract working as designed, not a
        # scaling regression in the tool/category projection this test
        # targets.
        tool = make_tool("reljqc-tool", "RelJQC Tool", published_at=PAST)
        source = make_comparison(slug="reljqc-source", title="RelJQC Source", author=self.author)
        add_entry(source, tool, position=10, summary="S")
        source = publish(source, self.author)

        for i in range(6):
            c = make_comparison(slug=f"reljqc-small-{i}", title=f"RelJQC Small {i}", author=self.author)
            add_entry(c, tool, position=10, summary="S")
            publish(c, self.author)
        with CaptureQueriesContext(connection) as small_ctx:
            related_comparisons(Comparison.objects.get(pk=source.pk), limit=6, language_code="en")

        for i in range(20):
            c = make_comparison(slug=f"reljqc-large-{i}", title=f"RelJQC Large {i}", author=self.author)
            add_entry(c, tool, position=10, summary="S")
            publish(c, self.author)
        with CaptureQueriesContext(connection) as large_ctx:
            related_comparisons(Comparison.objects.get(pk=source.pk), limit=6, language_code="en")

        self.assertEqual(len(small_ctx.captured_queries), len(large_ctx.captured_queries))
        self.assertLessEqual(len(large_ctx.captured_queries), 8)


class DataIntegrityTests(RelatedLiveToolsTestCase):
    """Group M."""

    def test_repeated_calls_do_not_mutate_anything(self):
        tool = make_tool("reljintegrity-tool", "RelJIntegrity Tool", published_at=PAST)
        source = make_comparison(slug="reljintegrity-source", title="RelJIntegrity Source", author=self.author)
        add_entry(source, tool, position=10, summary="S")
        source = publish(source, self.author)

        candidate = make_comparison(slug="reljintegrity-candidate", title="RelJIntegrity Candidate", author=self.author)
        add_entry(candidate, tool, position=10, summary="S")
        candidate = publish(candidate, self.author)

        before = Comparison.objects.get(pk=candidate.pk)
        before_snapshot = before.live_entries
        before_i18n = before.live_i18n
        before_status = before.status
        before_updated = before.updated_at
        before_draft = list(before.tool_entries.values_list("pk", "position", "tool_id"))

        for _ in range(3):
            related_comparisons(Comparison.objects.get(pk=source.pk), limit=6, language_code="en")
            self.client.get("/en/compare/reljintegrity-source/")

        after = Comparison.objects.get(pk=candidate.pk)
        self.assertEqual(after.live_entries, before_snapshot)
        self.assertEqual(after.live_i18n, before_i18n)
        self.assertEqual(after.status, before_status)
        self.assertEqual(after.updated_at, before_updated)
        self.assertEqual(list(after.tool_entries.values_list("pk", "position", "tool_id")), before_draft)
