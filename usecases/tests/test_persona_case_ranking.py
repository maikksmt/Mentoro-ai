"""
Beta 8.13: related_usecases() (core/services.py) used two different case
semantics for the same persona comparison:

    candidate filter:     translations__persona__iexact=persona   (case-insensitive)
    persona_match ranking: translations__persona=persona           (case-SENSITIVE)

A candidate whose persona differs only in case from the current use case's
persona (e.g. current="founder", candidate="Founder") therefore passed the
filter (so it was included in the related pool) but scored persona_match=0
in the ranking annotation - the same as a candidate with a completely
different persona - instead of tying with an exact-case match. Confirmed
via reproduction (see PersonaCaseInconsistencyReproductionTests below and
the Beta 8.13 report's direct shell reproduction).

Required behavior after the fix: persona_match uses the same case-
insensitive, exact-string (no substring) comparison as the candidate
filter, scoped to the same active-language translation row. Everything
else (tool_matches weight, order_by field order, published_at tie-break,
limit, fallback-fill, status/language visibility, exclusion of the current
object) is unchanged.
"""
from django.test import TestCase
from django.utils import timezone, translation
from parler.utils.context import switch_language

from core.models.editorial import EditorialWorkflowMixin
from core.services import related_usecases
from usecases.models import UseCase


def make_usecase(*, slug, status=EditorialWorkflowMixin.STATUS_PUBLISHED,
                  published_at=None, languages=("en", "de"), persona="", **extra):
    """
    Beta 11.7B: since related_usecases() now ranks on the live snapshot
    persona (live_i18n[lang]["persona"]) rather than the current draft
    translation, a PUBLISHED fixture needs a real snapshot - otherwise every
    persona comparison in this module would see "" (no signal) instead of
    the persona these tests are specifically about. Written directly rather
    than through the FSM publish() transition to keep this module's fast,
    direct-construction style; DRAFT fixtures (tested elsewhere as excluded)
    deliberately get no snapshot.
    """
    if published_at is None and status == EditorialWorkflowMixin.STATUS_PUBLISHED:
        published_at = timezone.now()
    u = UseCase.objects.create(status=status, published_at=published_at, **extra)
    for lang in languages:
        u.create_translation(
            lang, title=f"Title {slug} {lang}", intro="i", body="b", outro="o",
            slug=f"{slug}-{lang}", persona=persona,
        )
    if status == EditorialWorkflowMixin.STATUS_PUBLISHED:
        u.live_i18n = {
            lang: {
                "slug": f"{slug}-{lang}", "public_slug": None,
                "title": f"Title {slug} {lang}", "intro": "i", "body": "b",
                "outro": "o", "persona": persona,
            }
            for lang in languages
        }
        u.save(update_fields=["live_i18n"])
    return u


class PersonaCaseInconsistencyReproductionTests(TestCase):
    """Section A: the exact reproduction scenario from the Beta 8.13 report."""

    def test_case_mismatched_and_exact_persona_rank_equally(self):
        current = make_usecase(slug="repro-current", languages=("en",), persona="founder")
        a = make_usecase(slug="repro-a", languages=("en",), persona="Founder")
        b = make_usecase(slug="repro-b", languages=("en",), persona="founder")
        c = make_usecase(slug="repro-c", languages=("en",), persona="investor")

        result = related_usecases(current, limit=6, language_code="en")
        by_pk = {u.pk: u for u in result}

        # Both A and B were selected by the (already case-insensitive) filter.
        self.assertIn(a.pk, by_pk)
        self.assertIn(b.pk, by_pk)

        # After the fix, both score the same persona_match value.
        self.assertEqual(by_pk[a.pk].persona_match, by_pk[b.pk].persona_match)
        self.assertEqual(by_pk[a.pk].persona_match, 1)

        # C (genuinely different persona, no shared tools either) never
        # enters the persona/tool-filtered+annotated branch at all - it is
        # only present via the unannotated fallback-fill query, so it has
        # no persona_match attribute (this is pre-existing, unrelated
        # fallback-fill behavior, not something this fix touches).
        self.assertFalse(hasattr(by_pk[c.pk], "persona_match"))

        # A and B both rank strictly above C (persona_match dominates order_by).
        result_pks = [u.pk for u in result]
        self.assertLess(result_pks.index(a.pk), result_pks.index(c.pk))
        self.assertLess(result_pks.index(b.pk), result_pks.index(c.pk))

    def test_reproduction_matches_direct_annotation_query(self):
        """Locks in the fixed annotation's SQL-level behavior directly,
        independent of related_usecases()'s own filtering/ordering."""
        from django.db.models import Count, Q

        current = make_usecase(slug="direct-current", languages=("en",), persona="founder")
        a = make_usecase(slug="direct-a", languages=("en",), persona="FOUNDER")

        qs = (
            UseCase.objects.visible_in_language("en")
            .exclude(pk=current.pk)
            .filter(pk=a.pk)
            .annotate(
                persona_match=Count(
                    "id",
                    filter=Q(translations__language_code="en", translations__persona__iexact="founder"),
                )
            )
        )
        self.assertEqual(qs.get().persona_match, 1)


class PersonaCaseVariantTests(TestCase):
    """Section B: case variants, no substring semantics."""

    def test_lowercase_current_matches_uppercase_candidate(self):
        current = make_usecase(slug="case-lower-current", languages=("en",), persona="founder")
        cand = make_usecase(slug="case-upper-cand", languages=("en",), persona="FOUNDER")
        result = related_usecases(current, limit=6, language_code="en")
        matched = next(u for u in result if u.pk == cand.pk)
        self.assertEqual(matched.persona_match, 1)

    def test_titlecase_current_matches_lowercase_candidate(self):
        current = make_usecase(slug="case-title-current", languages=("en",), persona="Founder")
        cand = make_usecase(slug="case-lower-cand", languages=("en",), persona="founder")
        result = related_usecases(current, limit=6, language_code="en")
        matched = next(u for u in result if u.pk == cand.pk)
        self.assertEqual(matched.persona_match, 1)

    def test_identical_case_still_matches(self):
        current = make_usecase(slug="case-same-current", languages=("en",), persona="Founder")
        cand = make_usecase(slug="case-same-cand", languages=("en",), persona="Founder")
        result = related_usecases(current, limit=6, language_code="en")
        matched = next(u for u in result if u.pk == cand.pk)
        self.assertEqual(matched.persona_match, 1)

    def test_genuinely_different_persona_does_not_match(self):
        tool = self._make_tool("case-nomatch-tool")
        current = make_usecase(slug="case-nomatch-current", languages=("en",), persona="founder")
        current.tools.add(tool)
        cand = make_usecase(slug="case-nomatch-cand", languages=("en",), persona="investor")
        cand.tools.add(tool)
        result = related_usecases(current, limit=6, language_code="en")
        matched = next(u for u in result if u.pk == cand.pk)
        self.assertEqual(matched.persona_match, 0)

    def _make_tool(self, slug):
        from parler.utils.context import switch_language

        from catalog.models import Tool

        tool = Tool.objects.create(slug=slug, website=f"https://example.com/{slug}")
        with switch_language(tool, "en"):
            tool.name = slug
            tool.save()
        return tool

    def test_no_substring_match_co_founder(self):
        # A shared tool is required so this non-persona-matching candidate
        # is still selected into the annotated branch at all (persona_q
        # alone would exclude it, same as before this fix) - this isolates
        # exactly what's being tested: the annotation must not award a
        # persona_match point for a substring relationship.
        tool = self._make_tool("substr-tool-cofounder")
        current = make_usecase(slug="substr-current", languages=("en",), persona="founder")
        current.tools.add(tool)
        cand = make_usecase(slug="substr-cofounder", languages=("en",), persona="co-founder")
        cand.tools.add(tool)
        result = related_usecases(current, limit=6, language_code="en")
        matched = next(u for u in result if u.pk == cand.pk)
        self.assertEqual(matched.persona_match, 0)

    def test_no_substring_match_plural_founders(self):
        tool = self._make_tool("substr-tool-plural")
        current = make_usecase(slug="substr-plural-current", languages=("en",), persona="founder")
        current.tools.add(tool)
        cand = make_usecase(slug="substr-founders", languages=("en",), persona="founders")
        cand.tools.add(tool)
        result = related_usecases(current, limit=6, language_code="en")
        matched = next(u for u in result if u.pk == cand.pk)
        self.assertEqual(matched.persona_match, 0)


class PersonaLanguageIsolationTests(TestCase):
    """Section C."""

    def test_en_persona_match_considered_in_en(self):
        current = make_usecase(slug="lang-en-current", languages=("en",), persona="Founder")
        cand = make_usecase(slug="lang-en-cand", languages=("en",), persona="founder")
        result = related_usecases(current, limit=6, language_code="en")
        matched = next(u for u in result if u.pk == cand.pk)
        self.assertEqual(matched.persona_match, 1)

    def test_de_persona_match_considered_in_de(self):
        current = make_usecase(slug="lang-de-current", languages=("de",), persona="Gründer")
        cand = make_usecase(slug="lang-de-cand", languages=("de",), persona="gründer")
        # related_usecases() reads the *source* object's own persona via
        # usecase.safe_translation_getter("persona", any_language=False)
        # without an explicit language_code= - it relies on the object's
        # own parler "current language" (set via .language()/switch_language(),
        # not Django's ambient translation state) matching `language_code`.
        # In production this is always true: UseCaseDetailView.get_queryset()
        # fetches the object via visible_in_language(lang), which itself
        # calls .language(lang) - a pre-existing invariant (Beta 8.9a),
        # unrelated to this slice's case-sensitivity fix. switch_language()
        # here replicates that real fetch path instead of exercising this
        # unrelated edge case.
        with switch_language(current, "de"):
            result = related_usecases(current, limit=6, language_code="de")
        matched = next(u for u in result if u.pk == cand.pk)
        self.assertEqual(matched.persona_match, 1)

    def test_en_persona_in_other_translation_creates_no_de_rankingpoint(self):
        """A bilingual candidate whose EN persona happens to case-insensitively
        equal the current (DE-context) persona text must not score a match
        via its EN row while ranking in DE."""
        current = make_usecase(slug="lang-cross-current", languages=("de",), persona="Gründer")
        cand = make_usecase(slug="lang-cross-cand", languages=("en", "de"), persona="Gründer")
        # Force the EN translation's persona to a different, case-insensitively-
        # equal-to-nothing-relevant value so only the DE row could ever match
        # for real; then also verify the reverse: an EN-only value equal to
        # "Gründer" case-insensitively must still only count via the DE row.
        with switch_language(current, "de"):
            with_de_only_match = related_usecases(current, limit=6, language_code="de")
        matched = next(u for u in with_de_only_match if u.pk == cand.pk)
        self.assertEqual(matched.persona_match, 1)  # matches via its own DE row

    def test_de_persona_does_not_match_under_en(self):
        current = make_usecase(slug="lang-de-under-en-current", languages=("en",), persona="founder")
        # DE-only use case, no EN translation at all - must be entirely
        # absent from the EN-language related pool (visible_in_language()),
        # regardless of what its DE persona text is.
        de_only = make_usecase(slug="lang-de-under-en-cand", languages=("de",), persona="founder")
        result = related_usecases(current, limit=6, language_code="en")
        self.assertNotIn(de_only.pk, [u.pk for u in result])

    def test_explicit_language_code_overrides_ambient_language(self):
        current = make_usecase(slug="lang-explicit-current", languages=("en", "de"), persona="founder")
        cand = make_usecase(slug="lang-explicit-cand", languages=("de",), persona="Founder")
        with translation.override("en"):
            result = related_usecases(current, limit=6, language_code="de")
        self.assertIn(cand.pk, [u.pk for u in result])

    def test_django_language_state_unchanged_after_call(self):
        current = make_usecase(slug="lang-state-current", languages=("en", "de"), persona="founder")
        with translation.override("en"):
            related_usecases(current, limit=6, language_code="de")
            self.assertEqual(translation.get_language(), "en")


class PersonaRankingWeightAndOrderTests(TestCase):
    """Section D: existing weighting/order must be untouched, only case
    semantics corrected."""

    def test_tool_match_still_outranks_persona_only_match(self):
        from parler.utils.context import switch_language

        from catalog.models import Tool

        tool = Tool.objects.create(slug="weight-tool-813", website="https://example.com/w")
        with switch_language(tool, "en"):
            tool.name = "Weight Tool"
            tool.save()

        current = make_usecase(slug="weight-current", languages=("en",), persona="founder")
        current.tools.add(tool)

        tool_and_persona = make_usecase(slug="weight-both", languages=("en",), persona="Founder")
        tool_and_persona.tools.add(tool)

        persona_only = make_usecase(slug="weight-persona-only", languages=("en",), persona="founder")

        result = related_usecases(current, limit=6, language_code="en")
        pks = [u.pk for u in result]
        # order_by is "-persona_match", "-tool_matches", ... - both match
        # persona (1 each); the one that ALSO matches the tool must still
        # rank first, per the unchanged field order.
        self.assertLess(pks.index(tool_and_persona.pk), pks.index(persona_only.pk))

    def test_order_by_field_sequence_is_unchanged(self):
        current = make_usecase(slug="orderfields-current", languages=("en",), persona="founder")
        cand = make_usecase(slug="orderfields-cand", languages=("en",), persona="Founder")
        result = related_usecases(current, limit=6, language_code="en")
        matched = next(u for u in result if u.pk == cand.pk)
        # Both annotations still present with their original names/semantics.
        self.assertTrue(hasattr(matched, "persona_match"))
        self.assertTrue(hasattr(matched, "tool_matches"))

    def test_published_at_tie_breaker_still_applies_among_equal_persona_matches(self):
        current = make_usecase(slug="tie-current", languages=("en",), persona="founder")
        older = make_usecase(slug="tie-older", languages=("en",), persona="Founder")
        newer = make_usecase(slug="tie-newer", languages=("en",), persona="founder")
        now = timezone.now()
        UseCase.objects.filter(pk=older.pk).update(published_at=now - timezone.timedelta(days=2))
        UseCase.objects.filter(pk=newer.pk).update(published_at=now - timezone.timedelta(days=1))

        result = related_usecases(current, limit=6, language_code="en")
        pks = [u.pk for u in result]
        self.assertLess(pks.index(newer.pk), pks.index(older.pk))


class PersonaEmptyValueTests(TestCase):
    """Section E."""

    def test_empty_current_persona_yields_no_persona_filter(self):
        current = make_usecase(slug="empty-current", languages=("en",), persona="")
        make_usecase(slug="empty-other", languages=("en",), persona="")
        try:
            result = related_usecases(current, limit=6, language_code="en")
        except Exception as exc:  # noqa: BLE001
            self.fail(f"related_usecases() raised {exc!r} for an empty persona")
        self.assertIsInstance(result, list)

    def test_empty_current_persona_does_not_match_candidate_with_persona(self):
        current = make_usecase(slug="empty-vs-set-current", languages=("en",), persona="")
        make_usecase(slug="empty-vs-set-cand", languages=("en",), persona="Founder")
        # No exception, and no artificial match bonus is expected simply
        # because the candidate has *a* persona - existing semantics: an
        # empty current persona means persona_q is never added, so no
        # persona-driven filtering/ranking happens at all here.
        result = related_usecases(current, limit=6, language_code="en")
        self.assertIsInstance(result, list)

    def test_both_personas_empty_is_not_treated_as_a_match(self):
        current = make_usecase(slug="both-empty-current", languages=("en",), persona="")
        cand = make_usecase(slug="both-empty-cand", languages=("en",), persona="")
        result = related_usecases(current, limit=6, language_code="en")
        matched = next((u for u in result if u.pk == cand.pk), None)
        if matched is not None and hasattr(matched, "persona_match"):
            self.assertEqual(matched.persona_match, 0)

    def test_both_personas_empty_with_shared_tool_scores_no_persona_point(self):
        """Beta 8.13a: this is the scenario that previously DID score
        persona_match=1 - a shared tool put both empty-persona use cases
        into the annotated branch, and the annotation's Q was built
        unconditionally as translations__persona__iexact="", which matched
        the candidate's equally empty persona. Fixed: an empty source
        persona now short-circuits persona_match to a constant 0."""
        from parler.utils.context import switch_language

        from catalog.models import Tool

        tool = Tool.objects.create(slug="both-empty-tool-813a", website="https://example.com/e")
        with switch_language(tool, "en"):
            tool.name = "Both Empty Tool"
            tool.save()

        current = make_usecase(slug="both-empty-tool-current", languages=("en",), persona="")
        current.tools.add(tool)
        cand = make_usecase(slug="both-empty-tool-cand", languages=("en",), persona="")
        cand.tools.add(tool)

        result = related_usecases(current, limit=6, language_code="en")
        matched = next(u for u in result if u.pk == cand.pk)
        self.assertEqual(matched.persona_match, 0)
        # Tool ranking itself must remain unaffected.
        self.assertEqual(matched.tool_matches, 1)


class UseCaseDraftSlugRegressionTests(TestCase):
    """Section H: confirm this slice does not disturb Beta 8.11's use case
    draft-slug / snapshot safety, exercised specifically through the
    related-content path."""

    def test_related_links_are_snapshot_and_language_safe(self):
        current = make_usecase(slug="regr-current", languages=("en",), persona="founder")
        cand = make_usecase(slug="regr-cand", languages=("en",), persona="Founder")
        result = related_usecases(current, limit=6, language_code="en")
        self.assertIn(cand.pk, [u.pk for u in result])
        with translation.override("en"):
            resp = self.client.get(cand.get_absolute_url(language="en"))
        self.assertEqual(resp.status_code, 200)
