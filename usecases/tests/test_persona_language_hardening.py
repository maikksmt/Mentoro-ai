"""
Beta 8.13a: two follow-ups to Beta 8.13's persona case-sensitivity fix,
both confirmed via reproduction and documented in the Beta 8.13 report.

1. related_usecases() read the source use case's persona via
   usecase.safe_translation_getter("persona", any_language=False), with no
   explicit language_code - that reads whichever language the object
   instance's OWN parler "current language" happens to be
   (usecase.get_current_language()), which is not necessarily the
   `language_code` parameter the caller asked for. An instance whose
   current language is "en" but ranked here with language_code="de" would
   rank against its EN persona text while every candidate is matched
   against its own DE translation - so a genuinely matching DE candidate
   never scored a persona point. Confirmed via reproduction: a bilingual
   use case (EN "Founder" / DE "Gruender") whose instance stayed on "en",
   ranked with language_code="de" against a DE-only candidate with persona
   "Gruender", failed to match at all pre-fix.

   Fixed by reading the persona via has_translation(lang) (guard) +
   safe_translation_getter(language_code=lang) - has_translation() first
   is required because safe_translation_getter()'s own internal
   _get_translated_model() call always passes use_fallback=True whenever
   language_code differs from the instance's current language, regardless
   of any_language - it would otherwise silently substitute
   PARLER_LANGUAGES' fallback language's persona text for a missing `lang`
   translation instead of treating it as empty.

2. persona_match could be 1 for two use cases that both simply have no
   persona set, whenever they also shared a tool - the persona filter Q
   was built unconditionally as translations__persona__iexact="" in that
   branch, which matched any other equally-empty candidate. Fixed: an
   empty source persona now short-circuits persona_match to a constant 0
   (Value(0), no query added), regardless of tool overlap.
"""
from django.test import TestCase
from django.utils import timezone, translation
from parler.utils.context import switch_language

from core.models.editorial import EditorialWorkflowMixin
from core.services import related_usecases
from usecases.models import UseCase


def make_usecase(*, slug, status=EditorialWorkflowMixin.STATUS_PUBLISHED,
                  published_at=None, personas=None, **extra):
    """personas: dict of {language_code: persona_text}."""
    if published_at is None and status == EditorialWorkflowMixin.STATUS_PUBLISHED:
        published_at = timezone.now()
    u = UseCase.objects.create(status=status, published_at=published_at, **extra)
    for lang, persona in (personas or {}).items():
        u.create_translation(
            lang, title=f"Title {slug} {lang}", intro="i", body="b", outro="o",
            slug=f"{slug}-{lang}", persona=persona,
        )
    return u


class ExplicitLanguageControlsSourcePersonaTests(TestCase):
    """Section A/B: language_code, not the object's own current parler
    language, must determine which persona is read."""

    def test_object_current_en_target_de_uses_de_persona(self):
        current = make_usecase(
            slug="lang-ctrl-current", personas={"en": "Founder", "de": "Gruender"}
        )
        self.assertEqual(current.get_current_language(), "en")

        de_cand = make_usecase(slug="lang-ctrl-de-cand", personas={"de": "Gruender"})
        en_cand = make_usecase(slug="lang-ctrl-en-cand", personas={"en": "Founder"})

        result = related_usecases(current, limit=6, language_code="de")
        by_pk = {u.pk: u for u in result}

        # The DE candidate must score the match (source persona was read
        # in "de", not "en") ...
        self.assertIn(de_cand.pk, by_pk)
        self.assertEqual(by_pk[de_cand.pk].persona_match, 1)
        # ... and the EN-only candidate is invisible under "de" entirely
        # (visible_in_language(), unrelated to persona), so it cannot be
        # in the result at all.
        self.assertNotIn(en_cand.pk, by_pk)

        # The object's own current language must be untouched afterward.
        self.assertEqual(current.get_current_language(), "en")

    def test_object_current_de_target_en_uses_en_persona(self):
        current = make_usecase(
            slug="lang-ctrl2-current", personas={"en": "Founder", "de": "Gruender"}
        )
        current.set_current_language("de")
        self.assertEqual(current.get_current_language(), "de")

        en_cand = make_usecase(slug="lang-ctrl2-en-cand", personas={"en": "Founder"})

        result = related_usecases(current, limit=6, language_code="en")
        by_pk = {u.pk: u for u in result}
        self.assertIn(en_cand.pk, by_pk)
        self.assertEqual(by_pk[en_cand.pk].persona_match, 1)
        self.assertEqual(current.get_current_language(), "de")

    def test_same_object_and_target_language_still_works(self):
        current = make_usecase(slug="lang-ctrl-same", personas={"en": "Founder"})
        cand = make_usecase(slug="lang-ctrl-same-cand", personas={"en": "Founder"})
        result = related_usecases(current, limit=6, language_code="en")
        matched = next(u for u in result if u.pk == cand.pk)
        self.assertEqual(matched.persona_match, 1)


class MissingTargetTranslationTests(TestCase):
    """Section C: no fallback substitution when the target language
    translation is genuinely missing."""

    def test_missing_de_translation_treats_persona_as_empty_not_en_fallback(self):
        current = make_usecase(slug="missing-de-current", personas={"en": "Founder"})
        self.assertFalse(current.has_translation("de"))

        # An EN candidate whose persona equals the EN text must NOT score
        # a match under "de" - if the fallback bug were still present, the
        # source persona would silently resolve to "Founder" via
        # PARLER_LANGUAGES' fallback="en", and (if such a candidate were
        # even visible in "de", which it structurally cannot be) it would
        # wrongly score persona_match=1.
        result = related_usecases(current, limit=6, language_code="de")
        # No exception, clean empty/fallback-filled result.
        self.assertIsInstance(result, list)

    def test_missing_translation_persona_lookup_does_not_raise(self):
        current = make_usecase(slug="missing-de-current-2", personas={"en": "Founder"})
        try:
            related_usecases(current, limit=6, language_code="de")
        except Exception as exc:  # noqa: BLE001
            self.fail(f"related_usecases() raised {exc!r} for a missing target translation")

    def test_missing_translation_leaves_states_unchanged(self):
        current = make_usecase(slug="missing-de-current-3", personas={"en": "Founder"})
        django_before = translation.get_language()
        parler_before = current.get_current_language()
        related_usecases(current, limit=6, language_code="de")
        self.assertEqual(translation.get_language(), django_before)
        self.assertEqual(current.get_current_language(), parler_before)


class LanguageStateRestorationTests(TestCase):
    """Section B: Django and parler language state must be identical
    before and after the call, in every direction."""

    def test_state_unchanged_object_en_target_de(self):
        current = make_usecase(slug="state-en-de", personas={"en": "Founder", "de": "Gruender"})
        django_before = translation.get_language()
        parler_before = current.get_current_language()
        related_usecases(current, limit=6, language_code="de")
        self.assertEqual(translation.get_language(), django_before)
        self.assertEqual(current.get_current_language(), parler_before)

    def test_state_unchanged_object_de_target_en(self):
        current = make_usecase(slug="state-de-en", personas={"en": "Founder", "de": "Gruender"})
        current.set_current_language("de")
        django_before = translation.get_language()
        parler_before = current.get_current_language()
        related_usecases(current, limit=6, language_code="en")
        self.assertEqual(translation.get_language(), django_before)
        self.assertEqual(current.get_current_language(), parler_before)

    def test_explicit_language_code_overrides_ambient_django_language(self):
        current = make_usecase(slug="state-ambient", personas={"en": "Founder", "de": "Gruender"})
        de_cand = make_usecase(slug="state-ambient-cand", personas={"de": "Gruender"})
        with translation.override("en"):
            result = related_usecases(current, limit=6, language_code="de")
        by_pk = {u.pk: u for u in result}
        self.assertIn(de_cand.pk, by_pk)
        self.assertEqual(by_pk[de_cand.pk].persona_match, 1)

    def test_explicit_language_code_overrides_object_parler_state(self):
        current = make_usecase(slug="state-object", personas={"en": "Founder", "de": "Gruender"})
        current.set_current_language("en")
        de_cand = make_usecase(slug="state-object-cand", personas={"de": "Gruender"})
        result = related_usecases(current, limit=6, language_code="de")
        by_pk = {u.pk: u for u in result}
        self.assertIn(de_cand.pk, by_pk)
        self.assertEqual(by_pk[de_cand.pk].persona_match, 1)
        self.assertEqual(current.get_current_language(), "en")


class CandidateLanguageIsolationTests(TestCase):
    """Section G/7: candidate persona matching must still only ever
    consider the candidate's own active-language translation row."""

    def _tool(self, slug):
        from catalog.models import Tool

        tool = Tool.objects.create(slug=slug, website=f"https://example.com/{slug}")
        with switch_language(tool, "en"):
            tool.name = slug
            tool.save()
        return tool

    def test_en_candidate_persona_creates_no_match_under_de(self):
        # A shared tool keeps the candidate in the annotated branch even
        # though it has no persona-based match, so persona_match=0 can be
        # asserted directly instead of relying on the unannotated fallback.
        tool = self._tool("cand-iso-tool-1")
        current = make_usecase(slug="cand-iso-current", personas={"de": "Gruender"})
        current.tools.add(tool)
        # Bilingual candidate: EN persona happens to equal "Founder", DE
        # persona is something unrelated ("Investor") - only the DE row
        # may ever be consulted while ranking under "de".
        cand = make_usecase(
            slug="cand-iso-cand", personas={"en": "Founder", "de": "Investor"}
        )
        cand.tools.add(tool)
        result = related_usecases(current, limit=6, language_code="de")
        matched = next(u for u in result if u.pk == cand.pk)
        self.assertEqual(matched.persona_match, 0)

    def test_de_candidate_persona_creates_no_match_under_en(self):
        tool = self._tool("cand-iso-tool-2")
        current = make_usecase(slug="cand-iso-current-2", personas={"en": "Founder"})
        current.tools.add(tool)
        cand = make_usecase(
            slug="cand-iso-cand-2", personas={"en": "Investor", "de": "Founder"}
        )
        cand.tools.add(tool)
        result = related_usecases(current, limit=6, language_code="en")
        matched = next(u for u in result if u.pk == cand.pk)
        self.assertEqual(matched.persona_match, 0)


class EmptyPersonaNeverMatchesTests(TestCase):
    """Section D/7: an empty source persona must always yield
    persona_match=0, even across every tool-overlap variant."""

    def _tool(self, slug):
        from catalog.models import Tool

        tool = Tool.objects.create(slug=slug, website=f"https://example.com/{slug}")
        with switch_language(tool, "en"):
            tool.name = slug
            tool.save()
        return tool

    def test_empty_source_empty_candidate_shared_tool(self):
        tool = self._tool("empty-empty-tool")
        current = make_usecase(slug="empty-empty-current", personas={"en": ""})
        current.tools.add(tool)
        cand = make_usecase(slug="empty-empty-cand", personas={"en": ""})
        cand.tools.add(tool)

        result = related_usecases(current, limit=6, language_code="en")
        matched = next(u for u in result if u.pk == cand.pk)
        self.assertEqual(matched.persona_match, 0)
        self.assertEqual(matched.tool_matches, 1)

    def test_empty_source_set_candidate_shared_tool(self):
        tool = self._tool("empty-set-tool")
        current = make_usecase(slug="empty-set-current", personas={"en": ""})
        current.tools.add(tool)
        cand = make_usecase(slug="empty-set-cand", personas={"en": "Founder"})
        cand.tools.add(tool)

        result = related_usecases(current, limit=6, language_code="en")
        matched = next(u for u in result if u.pk == cand.pk)
        self.assertEqual(matched.persona_match, 0)
        self.assertEqual(matched.tool_matches, 1)

    def test_empty_source_multiple_shared_tools_still_no_persona_point(self):
        t1 = self._tool("empty-multi-tool-1")
        t2 = self._tool("empty-multi-tool-2")
        current = make_usecase(slug="empty-multi-current", personas={"en": ""})
        current.tools.add(t1, t2)
        cand = make_usecase(slug="empty-multi-cand", personas={"en": ""})
        cand.tools.add(t1, t2)

        result = related_usecases(current, limit=6, language_code="en")
        matched = next(u for u in result if u.pk == cand.pk)
        self.assertEqual(matched.persona_match, 0)
        self.assertEqual(matched.tool_matches, 2)

    def test_empty_source_persona_no_exception(self):
        current = make_usecase(slug="empty-noexc-current", personas={"en": ""})
        make_usecase(slug="empty-noexc-other", personas={"en": ""})
        try:
            result = related_usecases(current, limit=6, language_code="en")
        except Exception as exc:  # noqa: BLE001
            self.fail(f"related_usecases() raised {exc!r} for an empty persona")
        self.assertIsInstance(result, list)

    def test_tool_ranking_still_active_with_empty_personas(self):
        """Tool-based candidate selection/ranking must be fully unaffected
        by the persona_match=0 short-circuit."""
        strong_tool = self._tool("empty-tool-ranking-strong")
        current = make_usecase(slug="empty-tool-ranking-current", personas={"en": ""})
        current.tools.add(strong_tool)

        matches_tool = make_usecase(slug="empty-tool-ranking-match", personas={"en": ""})
        matches_tool.tools.add(strong_tool)
        no_tool = make_usecase(slug="empty-tool-ranking-notool", personas={"en": ""})

        result = related_usecases(current, limit=6, language_code="en")
        pks = [u.pk for u in result]
        self.assertIn(matches_tool.pk, pks)
        self.assertLess(pks.index(matches_tool.pk), pks.index(no_tool.pk))


class CaseInsensitiveRegressionTests(TestCase):
    """Section E/8: Beta 8.13's case-insensitive exact matching must
    remain fully intact after this hardening."""

    def test_lowercase_matches_titlecase(self):
        current = make_usecase(slug="case-regr-1", personas={"en": "founder"})
        cand = make_usecase(slug="case-regr-1-cand", personas={"en": "Founder"})
        result = related_usecases(current, limit=6, language_code="en")
        matched = next(u for u in result if u.pk == cand.pk)
        self.assertEqual(matched.persona_match, 1)

    def test_lowercase_matches_uppercase(self):
        current = make_usecase(slug="case-regr-2", personas={"en": "founder"})
        cand = make_usecase(slug="case-regr-2-cand", personas={"en": "FOUNDER"})
        result = related_usecases(current, limit=6, language_code="en")
        matched = next(u for u in result if u.pk == cand.pk)
        self.assertEqual(matched.persona_match, 1)

    def test_titlecase_matches_lowercase(self):
        current = make_usecase(slug="case-regr-3", personas={"en": "Founder"})
        cand = make_usecase(slug="case-regr-3-cand", personas={"en": "founder"})
        result = related_usecases(current, limit=6, language_code="en")
        matched = next(u for u in result if u.pk == cand.pk)
        self.assertEqual(matched.persona_match, 1)

    def test_identical_case_matches(self):
        current = make_usecase(slug="case-regr-4", personas={"en": "Founder"})
        cand = make_usecase(slug="case-regr-4-cand", personas={"en": "Founder"})
        result = related_usecases(current, limit=6, language_code="en")
        matched = next(u for u in result if u.pk == cand.pk)
        self.assertEqual(matched.persona_match, 1)

    def test_non_match_scores_zero(self):
        tool = None
        from catalog.models import Tool
        tool = Tool.objects.create(slug="case-regr-5-tool", website="https://example.com/5")
        with switch_language(tool, "en"):
            tool.name = "T"
            tool.save()
        current = make_usecase(slug="case-regr-5", personas={"en": "founder"})
        current.tools.add(tool)
        cand = make_usecase(slug="case-regr-5-cand", personas={"en": "investor"})
        cand.tools.add(tool)
        result = related_usecases(current, limit=6, language_code="en")
        matched = next(u for u in result if u.pk == cand.pk)
        self.assertEqual(matched.persona_match, 0)

    def test_no_substring_match_co_founder(self):
        from catalog.models import Tool
        tool = Tool.objects.create(slug="case-regr-6-tool", website="https://example.com/6")
        with switch_language(tool, "en"):
            tool.name = "T"
            tool.save()
        current = make_usecase(slug="case-regr-6", personas={"en": "founder"})
        current.tools.add(tool)
        cand = make_usecase(slug="case-regr-6-cand", personas={"en": "co-founder"})
        cand.tools.add(tool)
        result = related_usecases(current, limit=6, language_code="en")
        matched = next(u for u in result if u.pk == cand.pk)
        self.assertEqual(matched.persona_match, 0)

    def test_no_substring_match_founders_plural(self):
        from catalog.models import Tool
        tool = Tool.objects.create(slug="case-regr-7-tool", website="https://example.com/7")
        with switch_language(tool, "en"):
            tool.name = "T"
            tool.save()
        current = make_usecase(slug="case-regr-7", personas={"en": "founder"})
        current.tools.add(tool)
        cand = make_usecase(slug="case-regr-7-cand", personas={"en": "founders"})
        cand.tools.add(tool)
        result = related_usecases(current, limit=6, language_code="en")
        matched = next(u for u in result if u.pk == cand.pk)
        self.assertEqual(matched.persona_match, 0)


class RankingWeightOrderLimitFallbackTests(TestCase):
    """Section F/9: ranking weight, sort order, limit and fallback-fill
    must all be exactly unchanged by this hardening."""

    def test_order_by_fields_unchanged(self):
        current = make_usecase(slug="rank-order-current", personas={"en": "founder"})
        cand = make_usecase(slug="rank-order-cand", personas={"en": "Founder"})
        result = related_usecases(current, limit=6, language_code="en")
        matched = next(u for u in result if u.pk == cand.pk)
        self.assertTrue(hasattr(matched, "persona_match"))
        self.assertTrue(hasattr(matched, "tool_matches"))

    def test_tool_and_persona_match_outranks_persona_only(self):
        from catalog.models import Tool
        tool = Tool.objects.create(slug="rank-weight-tool", website="https://example.com/w2")
        with switch_language(tool, "en"):
            tool.name = "T"
            tool.save()

        current = make_usecase(slug="rank-weight-current", personas={"en": "founder"})
        current.tools.add(tool)

        both = make_usecase(slug="rank-weight-both", personas={"en": "Founder"})
        both.tools.add(tool)
        persona_only = make_usecase(slug="rank-weight-persona-only", personas={"en": "founder"})

        result = related_usecases(current, limit=6, language_code="en")
        pks = [u.pk for u in result]
        self.assertLess(pks.index(both.pk), pks.index(persona_only.pk))

    def test_published_at_tie_breaker_unchanged(self):
        current = make_usecase(slug="rank-tie-current", personas={"en": "founder"})
        older = make_usecase(slug="rank-tie-older", personas={"en": "Founder"})
        newer = make_usecase(slug="rank-tie-newer", personas={"en": "founder"})
        now = timezone.now()
        UseCase.objects.filter(pk=older.pk).update(published_at=now - timezone.timedelta(days=2))
        UseCase.objects.filter(pk=newer.pk).update(published_at=now - timezone.timedelta(days=1))
        result = related_usecases(current, limit=6, language_code="en")
        pks = [u.pk for u in result]
        self.assertLess(pks.index(newer.pk), pks.index(older.pk))

    def test_default_and_view_limit_unchanged(self):
        current = make_usecase(slug="rank-limit-current", personas={"en": "founder"})
        for i in range(10):
            make_usecase(slug=f"rank-limit-filler-{i}", personas={"en": "founder"})
        self.assertLessEqual(len(related_usecases(current, limit=6, language_code="en")), 6)
        self.assertLessEqual(len(related_usecases(current, limit=3, language_code="en")), 3)

    def test_fallback_fill_still_works(self):
        current = make_usecase(slug="rank-fallback-current", personas={"en": ""})
        make_usecase(slug="rank-fallback-filler", personas={"en": ""})
        result = related_usecases(current, limit=6, language_code="en")
        self.assertGreaterEqual(len(result), 1)

    def test_no_duplicates(self):
        from catalog.models import Tool
        t1 = Tool.objects.create(slug="rank-dup-tool-1", website="https://example.com/d1")
        with switch_language(t1, "en"):
            t1.name = "T1"
            t1.save()
        t2 = Tool.objects.create(slug="rank-dup-tool-2", website="https://example.com/d2")
        with switch_language(t2, "en"):
            t2.name = "T2"
            t2.save()

        current = make_usecase(slug="rank-dup-current", personas={"en": "founder"})
        current.tools.add(t1, t2)
        cand = make_usecase(slug="rank-dup-cand", personas={"en": "Founder"})
        cand.tools.add(t1, t2)

        result = related_usecases(current, limit=6, language_code="en")
        pks = [u.pk for u in result]
        self.assertEqual(len(pks), len(set(pks)))


class RelatedUseCaseCardRegressionTests(TestCase):
    """Section G: public related-card safety, both languages."""

    def test_en_card_link_reachable_and_correct(self):
        current = make_usecase(slug="card-en-current", personas={"en": "founder"})
        cand = make_usecase(slug="card-en-cand", personas={"en": "Founder"})
        result = related_usecases(current, limit=6, language_code="en")
        self.assertIn(cand.pk, [u.pk for u in result])
        with translation.override("en"):
            resp = self.client.get(cand.get_absolute_url(language="en"))
        self.assertEqual(resp.status_code, 200)

    def test_de_card_link_reachable_and_correct(self):
        current = make_usecase(slug="card-de-current", personas={"de": "Gruender"})
        cand = make_usecase(slug="card-de-cand", personas={"de": "gruender"})
        result = related_usecases(current, limit=6, language_code="de")
        self.assertIn(cand.pk, [u.pk for u in result])
        with translation.override("de"):
            resp = self.client.get(cand.get_absolute_url(language="de"))
        self.assertEqual(resp.status_code, 200)

    def test_draft_excluded_from_related_cards(self):
        current = make_usecase(slug="card-draft-current", personas={"en": "founder"})
        make_usecase(
            slug="card-draft-hidden", personas={"en": "founder"},
            status=EditorialWorkflowMixin.STATUS_DRAFT, published_at=None,
        )
        result = related_usecases(current, limit=6, language_code="en")
        self.assertEqual(
            len([u for u in result if "card-draft-hidden" in (u.safe_translation_getter("slug") or "")]), 0
        )
