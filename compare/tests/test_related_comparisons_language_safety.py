"""
Beta 8.11a: reproduction of the confirmed related_comparisons() language
leak, documented in the Beta 8.11 final report.

related_comparisons() (core/services.py) used Comparison.published (the
PublishedOnlyManager) with NO explicit language filter at all - not even
.language(lang)/.translated(lang). PublishedOnlyManager.get_queryset()
calls .active_translations(get_language()) internally, which - given
PARLER_LANGUAGES' hide_untranslated=False (mentoroai/settings/base.py) -
does not restrict the queryset to objects with a genuine translation in
the active language; it lets fallback-translated objects through too.

Beta 8.11's snapshot-safe teaser resolver (_public_teaser_value(),
Comparison.get_absolute_url()) correctly refuses to substitute another
language's title/slug for such an object - but that just means the
resulting teaser dict has title="" and url="#" instead of a wrong-language
title/URL. The card itself was never excluded from the related list, so
templates/partials/_teaser_card.html (which renders unconditionally, with
no title/url guard - by design, see Beta 8.11a's "service filters, template
does not" architecture) still rendered an empty, unclickable card.

Confirmed behavior BEFORE the fix:
    EN-only comparison selected as a "related" card on a DE page
    -> title == "", url == "#" (not a 404 link, but an empty dead card
       occupying a slot that should have gone to a real DE-visible item)

Required behavior AFTER the fix:
    EN-only/DE-only comparisons never appear in the other language's
    related list at all.
"""
from django.test import TestCase
from django.utils import timezone, translation

from core.models.editorial import EditorialWorkflowMixin
from core.services import related_comparisons, to_teaser_item
from compare.models import Comparison


def make_comparison(*, slug, languages=("en", "de"),
                     status=EditorialWorkflowMixin.STATUS_PUBLISHED, published_at=None):
    if published_at is None and status == EditorialWorkflowMixin.STATUS_PUBLISHED:
        published_at = timezone.now()
    c = Comparison.objects.create(status=status, published_at=published_at)
    for lang in languages:
        c.create_translation(lang, title=f"Title {slug} {lang}", intro="i", body="b", slug=f"{slug}-{lang}")
    return c


class RelatedComparisonsLanguageLeakReproductionTests(TestCase):
    def test_en_only_candidate_absent_from_german_related_list(self):
        current = make_comparison(slug="current-de", languages=("de",))
        candidate = make_comparison(slug="strong-match-en-only", languages=("en",))
        # No shared tools/categories - the fallback-fill path is what
        # exposes the leak (qs.filter(...) alone would already exclude a
        # tool-less candidate under the *old* code too, since Q(tools__in=[])
        # matches nothing without tool overlap; the second, unfiltered
        # "always return something useful" fallback query is the one that
        # picked up ambient-language fallback objects).
        with translation.override("de"):
            rel = related_comparisons(current, limit=3, language_code="de")
        self.assertNotIn(candidate, rel)

    def test_de_only_candidate_absent_from_english_related_list(self):
        current = make_comparison(slug="current-en", languages=("en",))
        candidate = make_comparison(slug="strong-match-de-only", languages=("de",))
        with translation.override("en"):
            rel = related_comparisons(current, limit=3, language_code="en")
        self.assertNotIn(candidate, rel)

    def test_en_only_candidate_teaser_would_have_been_empty_and_hashed(self):
        """Documents the pre-fix symptom directly: even when a wrong-
        language candidate slipped through, its teaser was never a working
        card - title empty, url "#". This is a component-level replay of
        the same computation the service performed pre-fix (Comparison.
        published with no language filter), independent of today's fix,
        to keep the confirmed root cause on record."""
        candidate = make_comparison(slug="teaser-check-en-only", languages=("en",))
        with translation.override("de"):
            item = to_teaser_item(candidate, "comparison")
        self.assertEqual(item["title"], "")
        self.assertEqual(item["url"], "#")


class RelatedComparisonsBilingualTests(TestCase):
    def test_bilingual_candidate_appears_with_correct_title_and_slug_per_language(self):
        current = make_comparison(slug="current-bilingual", languages=("en", "de"))
        candidate = make_comparison(slug="bilingual-candidate", languages=("en", "de"))

        rel_en = related_comparisons(current, limit=6, language_code="en")
        rel_de = related_comparisons(current, limit=6, language_code="de")
        self.assertIn(candidate, rel_en)
        self.assertIn(candidate, rel_de)

        item_en = to_teaser_item(candidate, "comparison", language_code="en")
        item_de = to_teaser_item(candidate, "comparison", language_code="de")
        self.assertEqual(item_en["title"], "Title bilingual-candidate en")
        self.assertEqual(item_de["title"], "Title bilingual-candidate de")
        self.assertIn("bilingual-candidate-en", item_en["url"])
        self.assertIn("bilingual-candidate-de", item_de["url"])
        self.assertNotEqual(item_en["url"], "#")
        self.assertNotEqual(item_de["url"], "#")

    def test_bilingual_candidate_link_returns_200_in_both_languages(self):
        current = make_comparison(slug="current-bilingual-2", languages=("en", "de"))
        candidate = make_comparison(slug="bilingual-candidate-2", languages=("en", "de"))

        rel_en = related_comparisons(current, limit=6, language_code="en")
        self.assertIn(candidate, rel_en)
        item_en = to_teaser_item(candidate, "comparison", language_code="en")
        self.assertEqual(self.client.get(item_en["url"]).status_code, 200)

        rel_de = related_comparisons(current, limit=6, language_code="de")
        self.assertIn(candidate, rel_de)
        item_de = to_teaser_item(candidate, "comparison", language_code="de")
        self.assertEqual(self.client.get(item_de["url"]).status_code, 200)


class RelatedComparisonsStatusAndExclusionTests(TestCase):
    def test_draft_candidate_excluded(self):
        current = make_comparison(slug="current-status", languages=("en",))
        make_comparison(slug="draft-candidate", languages=("en",),
                         status=EditorialWorkflowMixin.STATUS_DRAFT, published_at=None)
        rel = related_comparisons(current, limit=6, language_code="en")
        self.assertEqual(len(rel), 0)

    def test_current_object_excluded_from_its_own_related_list(self):
        current = make_comparison(slug="self-exclude", languages=("en",))
        make_comparison(slug="self-exclude-filler", languages=("en",))
        rel = related_comparisons(current, limit=6, language_code="en")
        self.assertNotIn(current, rel)

    def test_historical_published_without_live_snapshot_remains_eligible(self):
        current = make_comparison(slug="current-historical", languages=("en",))
        candidate = make_comparison(slug="historical-candidate", languages=("en",))
        self.assertEqual(candidate.live_i18n, {})
        rel = related_comparisons(current, limit=6, language_code="en")
        self.assertIn(candidate, rel)


class RelatedComparisonsRankingAndLimitTests(TestCase):
    def test_tool_match_ranks_above_no_match(self):
        from catalog.models import Tool
        tool = Tool.objects.create(slug="shared-tool-811a")
        tool.create_translation("en", name="Shared Tool")

        current = make_comparison(slug="ranking-current", languages=("en",))
        current.tools.add(tool)

        strong = make_comparison(slug="ranking-strong", languages=("en",))
        strong.tools.add(tool)
        weak = make_comparison(slug="ranking-weak", languages=("en",))

        rel = related_comparisons(current, limit=6, language_code="en")
        self.assertLess(rel.index(strong), rel.index(weak))

    def test_limit_is_respected(self):
        current = make_comparison(slug="limit-current", languages=("en",))
        for i in range(6):
            make_comparison(slug=f"limit-filler-{i}", languages=("en",))
        rel = related_comparisons(current, limit=3, language_code="en")
        self.assertEqual(len(rel), 3)

    def test_no_duplicates(self):
        from catalog.models import Tool
        t1 = Tool.objects.create(slug="tool-one-811a")
        t1.create_translation("en", name="Tool One")
        t2 = Tool.objects.create(slug="tool-two-811a")
        t2.create_translation("en", name="Tool Two")

        current = make_comparison(slug="dup-current", languages=("en",))
        current.tools.add(t1, t2)

        candidate = make_comparison(slug="dup-candidate", languages=("en",))
        candidate.tools.add(t1, t2)  # matches on both tools -> potential join duplication

        rel = related_comparisons(current, limit=6, language_code="en")
        self.assertEqual(rel.count(candidate), 1)

    def test_fallback_fill_uses_language_safe_query_too(self):
        current = make_comparison(slug="fallback-current", languages=("en",))
        make_comparison(slug="fallback-de-only", languages=("de",))
        make_comparison(slug="fallback-en-filler", languages=("en",))
        rel = related_comparisons(current, limit=3, language_code="en")
        for c in rel:
            self.assertTrue(c.has_translation("en"))


class RelatedComparisonsLanguageStateTests(TestCase):
    def test_active_django_language_unchanged_after_call(self):
        current = make_comparison(slug="lang-state-current", languages=("en", "de"))
        with translation.override("en"):
            related_comparisons(current, limit=3, language_code="de")
            self.assertEqual(translation.get_language(), "en")

    def test_explicit_language_code_overrides_ambient_language(self):
        current = make_comparison(slug="lang-override-current", languages=("en", "de"))
        candidate = make_comparison(slug="lang-override-candidate", languages=("de",))
        with translation.override("en"):
            rel = related_comparisons(current, limit=6, language_code="de")
        self.assertIn(candidate, rel)
