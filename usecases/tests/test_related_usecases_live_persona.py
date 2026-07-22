"""
Beta 11.7B: related_usecases() must rank on the live snapshot persona
(live_i18n[lang]["persona"]), never the current draft translation - on
either the source object or the candidates.

The defect: after Beta 11.7 widened public visibility to review/approved/
rework, a use case's persona could be edited and sent for another editorial
round while it stayed publicly listed. Because related_usecases() still read
`usecase.safe_translation_getter("persona", ...)` (the source) and matched
candidates via `translations__persona__iexact` (their current translation),
editing a persona and sending it to review changed which candidates it
matched - and which candidates it appeared as a match for - before that edit
was ever published. Not a rendered-text leak, a *behavioural* one.

Fixtures use the real editorial workflow
(usecases/tests/live_visibility_fixtures.py) throughout, so every published
state has a genuine reversion-backed live_i18n snapshot - exactly the shape
production data has after the Beta 11.7A backfill.
"""
from django.conf import settings
from django.core.cache import cache
from django.test import TestCase
from django.utils import translation
from reversion.models import Version

from core.services import related_usecases
from usecases.models import UseCase
from usecases.tests.live_visibility_fixtures import (
    add_translation,
    archive,
    make_usecase,
    make_user,
    publish,
    save_draft_edit,
    start_review_round,
)


def request_rework(usecase, by):
    fresh = UseCase.objects.get(pk=usecase.pk)
    fresh.request_rework(by=by, note="")
    fresh.save()
    return UseCase.objects.get(pk=fresh.pk)


def ranked_pks(usecase, language_code="en", limit=6):
    return [u.pk for u in related_usecases(usecase, limit=limit, language_code=language_code)]


def persona_match_of(usecase, pk, language_code="en", limit=6):
    """
    The candidate's persona_match, or None if it is not in the result set.

    A candidate that matches neither persona nor tools can still appear via
    the unannotated fallback-fill branch (unchanged pre-existing behaviour,
    see related_usecases()'s own "fallback" step) - such an item carries no
    persona_match attribute at all, which correctly means "not matched", 0.
    """
    for u in related_usecases(usecase, limit=limit, language_code=language_code):
        if u.pk == pk:
            return getattr(u, "persona_match", 0)
    return None


class DraftPersonaDoesNotAffectRankingBaseTestCase(TestCase):
    """Shared fixture for groups A/B/C/D: one source, two persona-distinct
    candidates, so the persona signal alone decides who ranks first."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.author = make_user("uc-relpersona-author")

        self.source = make_usecase(
            slug="relpersona-source", title="RelPersona Source",
            persona="Developers", author=self.author,
        )
        self.source = publish(self.source, self.author)

        self.cand_a = make_usecase(
            slug="relpersona-cand-a", title="RelPersona Cand A",
            persona="Developers", author=self.author,
        )
        self.cand_a = publish(self.cand_a, self.author)

        self.cand_b = make_usecase(
            slug="relpersona-cand-b", title="RelPersona Cand B",
            persona="Marketing", author=self.author,
        )
        self.cand_b = publish(self.cand_b, self.author)

    def _reread_source(self):
        return UseCase.objects.get(pk=self.source.pk)


class GroupA_ReviewTests(DraftPersonaDoesNotAffectRankingBaseTestCase):
    def test_review_with_draft_persona_change_ranks_by_live_persona(self):
        pks_before = ranked_pks(self._reread_source())
        self.assertEqual(pks_before.index(self.cand_a.pk), 0)

        save_draft_edit(self.source, "en", persona="Marketing")
        start_review_round(self.source, self.author)

        pks_after = ranked_pks(self._reread_source())
        self.assertEqual(pks_after.index(self.cand_a.pk), 0)
        self.assertEqual(persona_match_of(self._reread_source(), self.cand_a.pk), 1)

    def test_review_candidate_does_not_get_an_early_bonus_for_its_own_draft(self):
        save_draft_edit(self.cand_b, "en", persona="Developers")
        start_review_round(self.cand_b, self.author)

        self.assertEqual(persona_match_of(self._reread_source(), self.cand_b.pk), 0)


class GroupB_ReworkTests(DraftPersonaDoesNotAffectRankingBaseTestCase):
    def test_rework_with_draft_persona_change_ranks_by_live_persona(self):
        save_draft_edit(self.source, "en", persona="Marketing")
        start_review_round(self.source, self.author)
        reworked = request_rework(self.source, self.author)

        pks = ranked_pks(reworked)
        self.assertEqual(pks.index(self.cand_a.pk), 0)
        self.assertEqual(persona_match_of(reworked, self.cand_a.pk), 1)
        self.assertEqual(persona_match_of(reworked, self.cand_b.pk), 0)


class GroupC_ApprovedTests(DraftPersonaDoesNotAffectRankingBaseTestCase):
    def test_approved_with_draft_persona_change_ranks_by_live_persona(self):
        save_draft_edit(self.source, "en", persona="Marketing")
        fresh = UseCase.objects.get(pk=self.source.pk)
        fresh.move_to_review(by=self.author)
        fresh.save()
        fresh.approve(by=self.author)
        fresh.save()
        approved = UseCase.objects.get(pk=fresh.pk)

        pks = ranked_pks(approved)
        self.assertEqual(pks.index(self.cand_a.pk), 0)


class GroupD_RepublishTests(DraftPersonaDoesNotAffectRankingBaseTestCase):
    def test_republish_activates_the_new_persona_for_ranking(self):
        before = ranked_pks(self._reread_source())
        self.assertEqual(before.index(self.cand_a.pk), 0)

        save_draft_edit(self.source, "en", persona="Marketing")
        in_review = start_review_round(self.source, self.author)
        republished = publish(in_review, self.author)

        after = ranked_pks(republished)
        self.assertEqual(after.index(self.cand_b.pk), 0)
        self.assertEqual(persona_match_of(republished, self.cand_b.pk), 1)
        self.assertEqual(persona_match_of(republished, self.cand_a.pk), 0)


class GroupE_SourceAndCandidateSymmetryTests(TestCase):
    """Both sides of the comparison must read the live snapshot."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.author = make_user("uc-relpersona-symmetry-author")

    def test_source_uses_its_own_live_persona(self):
        source = make_usecase(
            slug="sym-source", title="Sym Source", persona="Developers", author=self.author
        )
        source = publish(source, self.author)
        cand = make_usecase(
            slug="sym-cand", title="Sym Cand", persona="Developers", author=self.author
        )
        cand = publish(cand, self.author)

        self.assertEqual(persona_match_of(source, cand.pk), 1)

        # Draft persona changes to "Marketing", but the live snapshot - and
        # therefore the ranking - stays on "Developers" until republished.
        save_draft_edit(source, "en", persona="Marketing")
        start_review_round(source, self.author)
        self.assertEqual(persona_match_of(UseCase.objects.get(pk=source.pk), cand.pk), 1)

    def test_candidate_uses_its_own_live_persona(self):
        source = make_usecase(
            slug="sym2-source", title="Sym2 Source", persona="Developers", author=self.author
        )
        source = publish(source, self.author)
        cand = make_usecase(
            slug="sym2-cand", title="Sym2 Cand", persona="Developers", author=self.author
        )
        cand = publish(cand, self.author)

        self.assertEqual(persona_match_of(source, cand.pk), 1)

        # The candidate's own draft persona changes; its published persona
        # (what the source is compared against) does not.
        save_draft_edit(cand, "en", persona="Marketing")
        start_review_round(cand, self.author)

        self.assertEqual(persona_match_of(UseCase.objects.get(pk=source.pk), cand.pk), 1)


class GroupF_LanguageIsolationTests(TestCase):
    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.author = make_user("uc-relpersona-lang-author")

    def _bilingual(self, slug, title, en_persona, de_persona):
        usecase = make_usecase(
            slug=f"{slug}-en", title=f"{title} EN", persona=en_persona, author=self.author
        )
        add_translation(usecase, "de", slug=f"{slug}-de", title=f"{title} DE", persona=de_persona)
        return publish(usecase, self.author)

    def test_en_ranking_uses_the_en_live_persona(self):
        source = self._bilingual("lang-source", "Lang Source", "Developers", "Entwickler")
        cand_en = self._bilingual("lang-cand-en", "Lang Cand EN", "Developers", "Andere")
        cand_de = self._bilingual("lang-cand-de", "Lang Cand DE", "Andere", "Entwickler")

        self.assertEqual(persona_match_of(source, cand_en.pk, "en"), 1)
        self.assertEqual(persona_match_of(source, cand_de.pk, "en"), 0)

    def test_de_ranking_uses_the_de_live_persona(self):
        source = self._bilingual("lang2-source", "Lang2 Source", "Developers", "Entwickler")
        cand_en = self._bilingual("lang2-cand-en", "Lang2 Cand EN", "Developers", "Andere")
        cand_de = self._bilingual("lang2-cand-de", "Lang2 Cand DE", "Andere", "Entwickler")

        self.assertEqual(persona_match_of(source, cand_en.pk, "de"), 0)
        self.assertEqual(persona_match_of(source, cand_de.pk, "de"), 1)

    def test_en_draft_edit_does_not_change_de_ranking(self):
        source = self._bilingual("lang3-source", "Lang3 Source", "Developers", "Entwickler")
        cand_de = self._bilingual("lang3-cand-de", "Lang3 Cand DE", "Andere", "Entwickler")

        save_draft_edit(source, "en", persona="Marketing")
        start_review_round(source, self.author)

        self.assertEqual(
            persona_match_of(UseCase.objects.get(pk=source.pk), cand_de.pk, "de"), 1
        )

    def test_de_draft_edit_does_not_change_en_ranking(self):
        source = self._bilingual("lang4-source", "Lang4 Source", "Developers", "Entwickler")
        cand_en = self._bilingual("lang4-cand-en", "Lang4 Cand EN", "Developers", "Andere")

        save_draft_edit(source, "de", persona="Marketing")
        start_review_round(source, self.author)

        self.assertEqual(
            persona_match_of(UseCase.objects.get(pk=source.pk), cand_en.pk, "en"), 1
        )

    def test_no_language_falls_back_to_the_other(self):
        source = make_usecase(
            slug="lang5-source-en", title="Lang5 Source EN", persona="Developers", author=self.author
        )
        source = publish(source, self.author)
        # DE draft added after publish - no DE snapshot at all.
        add_translation(source, "de", slug="lang5-source-de", title="Lang5 Source DE", persona="Entwickler")

        cand = make_usecase(
            slug="lang5-cand-en", title="Lang5 Cand EN", persona="Developers", author=self.author
        )
        cand = publish(cand, self.author)
        add_translation(cand, "de", slug="lang5-cand-de", title="Lang5 Cand DE", persona="Entwickler")

        # No DE live snapshot for either side: DE ranking must see no persona
        # signal, never falling back to EN's live persona.
        refreshed = UseCase.objects.get(pk=source.pk)
        result = related_usecases(refreshed, limit=6, language_code="de")
        self.assertEqual([u.pk for u in result if u.pk == cand.pk], [])


class GroupG_MissingOrEmptyPersonaTests(TestCase):
    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.author = make_user("uc-relpersona-missing-author")

    def test_snapshot_without_a_persona_key_gives_no_persona_signal(self):
        source = make_usecase(
            slug="missingkey-source", title="MissingKey Source",
            persona="Developers", author=self.author,
        )
        source = publish(source, self.author)

        cand = make_usecase(
            slug="missingkey-cand", title="MissingKey Cand",
            persona="Developers", author=self.author,
        )
        cand = publish(cand, self.author)
        # Simulate a pre-Beta-11.7 snapshot: no "persona" key at all.
        stripped = dict(cand.live_i18n)
        stripped["en"] = {k: v for k, v in stripped["en"].items() if k != "persona"}
        UseCase.objects.filter(pk=cand.pk).update(live_i18n=stripped)

        self.assertEqual(persona_match_of(source, cand.pk), 0)

    def test_missing_persona_key_does_not_raise(self):
        source = make_usecase(
            slug="missingkey2-source", title="MissingKey2 Source",
            persona="Developers", author=self.author,
        )
        source = publish(source, self.author)
        stripped = dict(source.live_i18n)
        stripped["en"] = {k: v for k, v in stripped["en"].items() if k != "persona"}
        UseCase.objects.filter(pk=source.pk).update(live_i18n=stripped)

        try:
            result = related_usecases(UseCase.objects.get(pk=source.pk), limit=6, language_code="en")
        except Exception as exc:  # noqa: BLE001
            self.fail(f"related_usecases() raised {exc!r} for a missing persona key")
        self.assertIsInstance(result, list)

    def test_two_empty_live_personas_do_not_score_a_positive_match(self):
        source = make_usecase(
            slug="emptypersona-source", title="EmptyPersona Source",
            persona="", author=self.author,
        )
        source = publish(source, self.author)
        cand = make_usecase(
            slug="emptypersona-cand", title="EmptyPersona Cand",
            persona="", author=self.author,
        )
        cand = publish(cand, self.author)

        self.assertEqual(source.live_i18n["en"]["persona"], "")
        self.assertEqual(cand.live_i18n["en"]["persona"], "")

        result = related_usecases(source, limit=6, language_code="en")
        match = next((u for u in result if u.pk == cand.pk), None)
        if match is not None:
            # Two empty personas take the unannotated fallback-fill branch
            # (source has no persona signal, so persona_q is never built) -
            # no persona_match attribute at all, which correctly means "no
            # match", 0.
            self.assertEqual(getattr(match, "persona_match", 0), 0)


class GroupH_InvisibleCandidatesTests(TestCase):
    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.author = make_user("uc-relpersona-invisible-author")
        self.source = make_usecase(
            slug="invis-source", title="Invis Source", persona="Developers", author=self.author
        )
        self.source = publish(self.source, self.author)

    def test_never_published_candidate_with_matching_persona_is_excluded(self):
        never_published = make_usecase(
            slug="invis-neverpub", title="Invis NeverPub",
            persona="Developers", author=self.author,
        )
        result = related_usecases(self.source, limit=6, language_code="en")
        self.assertNotIn(never_published.pk, [u.pk for u in result])

    def test_archived_candidate_with_matching_live_persona_is_excluded(self):
        cand = make_usecase(
            slug="invis-archived", title="Invis Archived", persona="Developers", author=self.author
        )
        cand = publish(cand, self.author)
        archived = archive(cand, self.author)
        self.assertEqual(archived.live_i18n["en"]["persona"], "Developers")

        result = related_usecases(self.source, limit=6, language_code="en")
        self.assertNotIn(archived.pk, [u.pk for u in result])

    def test_candidate_without_a_live_snapshot_in_the_requested_language_is_excluded(self):
        cand = make_usecase(
            slug="invis-nosnap-en", title="Invis NoSnap EN", persona="Developers", author=self.author
        )
        cand = publish(cand, self.author)
        add_translation(cand, "de", slug="invis-nosnap-de", title="Invis NoSnap DE", persona="Entwickler")

        result = related_usecases(self.source, limit=6, language_code="de")
        self.assertNotIn(cand.pk, [u.pk for u in result])

    def test_candidate_with_only_a_draft_translation_of_the_language_is_excluded(self):
        """A candidate whose German row is a fresh, never-published draft
        translation must not appear under a German ranking at all - visible_
        in_language() excludes it before persona matching ever runs."""
        cand = make_usecase(
            slug="invis-draftonly-en", title="Invis DraftOnly EN",
            persona="Developers", author=self.author,
        )
        cand = publish(cand, self.author)
        add_translation(
            cand, "de", slug="invis-draftonly-de", title="Invis DraftOnly DE", persona="Developers"
        )
        self.assertNotIn("de", UseCase.objects.get(pk=cand.pk).live_i18n)

        result = related_usecases(self.source, limit=6, language_code="de")
        self.assertNotIn(cand.pk, [u.pk for u in result])


class GroupI_OtherRankingFactorsUnchangedTests(TestCase):
    """Category/tool/tag-equivalent factors and tie-breaking must be exactly
    as before - only the persona data source changed."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.author = make_user("uc-relpersona-factors-author")

    def _tool(self, slug):
        from catalog.models import Tool
        from parler.utils.context import switch_language

        tool = Tool.objects.create(slug=slug, website=f"https://example.com/{slug}")
        with switch_language(tool, "en"):
            tool.name = slug
            tool.save()
        return tool

    def test_tool_weight_still_outranks_persona_only(self):
        tool = self._tool("relpersona-tool")
        source = make_usecase(
            slug="factors-source", title="Factors Source", persona="Developers", author=self.author
        )
        source = publish(source, self.author)
        source.tools.add(tool)

        both = make_usecase(
            slug="factors-both", title="Factors Both", persona="Developers", author=self.author
        )
        both = publish(both, self.author)
        both.tools.add(tool)

        persona_only = make_usecase(
            slug="factors-persona-only", title="Factors PersonaOnly",
            persona="Developers", author=self.author,
        )
        publish(persona_only, self.author)

        pks = ranked_pks(UseCase.objects.get(pk=source.pk))
        self.assertLess(pks.index(both.pk), pks.index(persona_only.pk))

    def test_published_at_tie_break_is_unchanged(self):
        source = make_usecase(
            slug="factors-tie-source", title="Factors Tie Source",
            persona="Developers", author=self.author,
        )
        source = publish(source, self.author)

        older = make_usecase(
            slug="factors-tie-older", title="Factors Tie Older",
            persona="Developers", author=self.author,
        )
        older = publish(older, self.author)
        newer = make_usecase(
            slug="factors-tie-newer", title="Factors Tie Newer",
            persona="Developers", author=self.author,
        )
        newer = publish(newer, self.author)

        from django.utils import timezone
        now = timezone.now()
        UseCase.objects.filter(pk=older.pk).update(published_at=now - timezone.timedelta(days=2))
        UseCase.objects.filter(pk=newer.pk).update(published_at=now - timezone.timedelta(days=1))

        pks = ranked_pks(UseCase.objects.get(pk=source.pk))
        self.assertLess(pks.index(newer.pk), pks.index(older.pk))

    def test_result_limit_is_unchanged(self):
        source = make_usecase(
            slug="factors-limit-source", title="Factors Limit Source",
            persona="Developers", author=self.author,
        )
        source = publish(source, self.author)
        for i in range(10):
            filler = make_usecase(
                slug=f"factors-limit-filler-{i}", title=f"Factors Limit Filler {i}",
                persona="Developers", author=self.author,
            )
            publish(filler, self.author)

        self.assertLessEqual(len(ranked_pks(UseCase.objects.get(pk=source.pk), limit=6)), 6)
        self.assertLessEqual(len(ranked_pks(UseCase.objects.get(pk=source.pk), limit=3)), 3)


class GroupJ_DetailPageIntegrationTests(TestCase):
    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        cache.clear()
        self.addCleanup(cache.clear)
        self.author = make_user("uc-relpersona-detail-author")

        self.source = make_usecase(
            slug="detailint-source", title="DetailInt Source",
            persona="Developers", author=self.author,
        )
        self.source = publish(self.source, self.author)
        self.cand_a = make_usecase(
            slug="detailint-cand-a", title="DetailInt Cand A",
            persona="Developers", author=self.author,
        )
        self.cand_a = publish(self.cand_a, self.author)
        self.cand_b = make_usecase(
            slug="detailint-cand-b", title="DetailInt Cand B",
            persona="Marketing", author=self.author,
        )
        self.cand_b = publish(self.cand_b, self.author)

    def test_detail_page_related_cards_match_the_service_before_republish(self):
        save_draft_edit(self.source, "en", title="DetailInt Draft Title", persona="Marketing")
        start_review_round(self.source, self.author)

        resp = self.client.get("/en/usecases/detailint-source/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()

        service_order = ranked_pks(UseCase.objects.get(pk=self.source.pk), limit=3)
        card_titles = [str(item["title"]) for item in resp.context["similar"]]
        # The top service result (still Developers-ranked) must be the first card.
        top = UseCase.objects.get(pk=service_order[0])
        self.assertIn(top.live_i18n["en"]["title"], card_titles)

        self.assertNotIn("DetailInt Draft Title", html)

    def test_related_card_links_use_live_slugs(self):
        resp = self.client.get("/en/usecases/detailint-source/")
        for item in resp.context["similar"]:
            with self.subTest(url=item["url"]):
                self.assertNotIn("draft", item["url"])


class GroupK_DataIntegrityTests(TestCase):
    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.author = make_user("uc-relpersona-integrity-author")
        self.source = make_usecase(
            slug="integrity2-source", title="Integrity2 Source",
            persona="Developers", author=self.author,
        )
        self.source = publish(self.source, self.author)
        self.cand = make_usecase(
            slug="integrity2-cand", title="Integrity2 Cand",
            persona="Developers", author=self.author,
        )
        self.cand = publish(self.cand, self.author)

    def _state(self):
        s = UseCase.objects.get(pk=self.source.pk)
        c = UseCase.objects.get(pk=self.cand.pk)
        return (
            s.status, s.live_i18n, s.last_published_revision_id, s.updated_at,
            s.safe_translation_getter("title", language_code="en"),
            c.status, c.live_i18n, c.last_published_revision_id, c.updated_at,
            list(s.tools.values_list("pk", flat=True)),
            Version.objects.get_for_object(s).count(),
        )

    def test_calling_the_service_changes_nothing(self):
        before = self._state()
        related_usecases(UseCase.objects.get(pk=self.source.pk), limit=6, language_code="en")
        self.assertEqual(before, self._state())

    def test_viewing_the_detail_page_changes_nothing(self):
        before = self._state()
        self.client.get("/en/usecases/integrity2-source/")
        self.assertEqual(before, self._state())


class GroupL_NoExtraQueriesTests(TestCase):
    """The candidate-side persona comparison moved from a joined-table filter
    to a JSONField key-transform on the already-loaded queryset - it must not
    add a query per candidate."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.author = make_user("uc-relpersona-query-author")
        self.source = make_usecase(
            slug="query-source", title="Query Source", persona="Developers", author=self.author
        )
        self.source = publish(self.source, self.author)
        # All matching, and already at least `limit`, so the query count is
        # measured on the same (non-fallback-triggering) code path both
        # before and after - isolating the persona-match cost itself from
        # the pre-existing, unrelated fallback-fill branch's own query.
        for i in range(6):
            u = make_usecase(
                slug=f"query-cand-{i}", title=f"Query Cand {i}",
                persona="Developers", author=self.author,
            )
            publish(u, self.author)

    def test_query_count_does_not_scale_with_matching_candidate_count(self):
        from django.test.utils import CaptureQueriesContext
        from django.db import connection

        with CaptureQueriesContext(connection) as ctx:
            related_usecases(UseCase.objects.get(pk=self.source.pk), limit=6, language_code="en")
        query_count = len(ctx.captured_queries)

        for i in range(6, 12):
            u = make_usecase(
                slug=f"query-cand-{i}", title=f"Query Cand {i}",
                persona="Developers", author=self.author,
            )
            publish(u, self.author)

        with CaptureQueriesContext(connection) as ctx2:
            related_usecases(UseCase.objects.get(pk=self.source.pk), limit=6, language_code="en")
        self.assertEqual(len(ctx2.captured_queries), query_count)
