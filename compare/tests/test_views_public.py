"""
Beta 8.8/8.9: Comparison language behavior.

- List view (Beta 8.9): now uses Comparison.objects.visible_in_language(),
  strict, no cross-language fallback - fixes a real, empirically confirmed
  leak where the German list (via the old .published manager's
  active_translations() fallback) could show an EN-only comparison whose
  generated detail link then 404'd under Beta 8.8's strict
  ComparisonDetailView resolution.
- Detail view (Beta 8.8): the slug lookup requires the matched translation
  row to belong to the active language - unchanged in this slice.
- get_absolute_url()'s raw switch_language() + self.slug access could
  previously crash localized_alternates() for single-language objects; that
  entry point is now guarded generically in core/seo/utils.py (Beta 8.9),
  so single-language "reachable" fixtures no longer need a bilingual
  workaround.
"""
from django.test import TestCase, RequestFactory
from django.utils import timezone, translation

from compare.models import Comparison
from compare.views import ComparisonListView


def make_comparison(*, slug, languages=("en", "de")):
    c = Comparison.objects.create(status="published", published_at=timezone.now())
    for lang in languages:
        c.create_translation(lang, title=f"Title {slug} {lang}", intro="i", body="b", slug=f"{slug}-{lang}")
    return c


class ComparisonListLanguageIsolationTests(TestCase):
    """Beta 8.9: the list only shows comparisons with an actual translation
    in the active language - no more cross-language fallback leaks."""

    def _queryset_for(self, lang):
        with translation.override(lang):
            request = RequestFactory().get("/compare/")
            view = ComparisonListView()
            view.request = request
            return view.get_queryset()

    def test_en_only_comparison_absent_from_german_list(self):
        c = make_comparison(slug="en-only-list", languages=("en",))
        self.assertFalse(self._queryset_for("de").filter(pk=c.pk).exists())
        self.assertTrue(self._queryset_for("en").filter(pk=c.pk).exists())

    def test_de_only_comparison_absent_from_english_list(self):
        c = make_comparison(slug="de-only-list", languages=("de",))
        self.assertFalse(self._queryset_for("en").filter(pk=c.pk).exists())
        self.assertTrue(self._queryset_for("de").filter(pk=c.pk).exists())

    def test_bilingual_comparison_appears_in_both_lists(self):
        c = make_comparison(slug="bilingual-list", languages=("en", "de"))
        self.assertTrue(self._queryset_for("en").filter(pk=c.pk).exists())
        self.assertTrue(self._queryset_for("de").filter(pk=c.pk).exists())

    def test_draft_comparison_absent_from_every_language(self):
        c = Comparison.objects.create(status="draft", published_at=None)
        c.create_translation("en", title="Draft", intro="i", body="b", slug="draft-list-en")
        c.create_translation("de", title="Entwurf", intro="i", body="b", slug="draft-list-de")
        self.assertFalse(self._queryset_for("en").filter(pk=c.pk).exists())
        self.assertFalse(self._queryset_for("de").filter(pk=c.pk).exists())

    def test_every_listed_comparisons_detail_url_is_reachable(self):
        make_comparison(slug="reachable-en", languages=("en",))
        make_comparison(slug="reachable-de", languages=("de",))
        make_comparison(slug="reachable-both", languages=("en", "de"))
        for lang in ("en", "de"):
            for obj in self._queryset_for(lang):
                with translation.override(lang):
                    url = obj.get_absolute_url(language=lang)
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 200, f"{url} ({lang}) should be reachable")

    def test_no_duplicate_results(self):
        make_comparison(slug="dup-check", languages=("en", "de"))
        qs = self._queryset_for("en")
        self.assertEqual(qs.count(), len(list(qs)))


class ComparisonDetailLanguageTests(TestCase):
    def test_english_comparison_reachable_under_english_url(self):
        make_comparison(slug="en-detail", languages=("en",))
        resp = self.client.get("/en/compare/en-detail-en/")
        self.assertEqual(resp.status_code, 200)

    def test_german_comparison_reachable_under_german_url(self):
        make_comparison(slug="de-detail", languages=("de",))
        resp = self.client.get("/de/compare/de-detail-de/")
        self.assertEqual(resp.status_code, 200)

    def test_en_only_comparison_404s_under_german_prefix(self):
        make_comparison(slug="en-only-detail", languages=("en",))
        resp = self.client.get("/de/compare/en-only-detail-en/")
        self.assertEqual(resp.status_code, 404)

    def test_de_only_comparison_404s_under_english_prefix(self):
        make_comparison(slug="de-only-detail", languages=("de",))
        resp = self.client.get("/en/compare/de-only-detail-de/")
        self.assertEqual(resp.status_code, 404)

    def test_bilingual_comparison_english_slug_404s_under_german_prefix(self):
        make_comparison(slug="cross-slug", languages=("en", "de"))
        resp = self.client.get("/de/compare/cross-slug-en/")
        self.assertEqual(resp.status_code, 404)

    def test_bilingual_comparison_shows_correct_translation_per_language(self):
        make_comparison(slug="bilingual", languages=("en", "de"))
        resp_en = self.client.get("/en/compare/bilingual-en/")
        resp_de = self.client.get("/de/compare/bilingual-de/")
        self.assertEqual(resp_en.status_code, 200)
        self.assertEqual(resp_de.status_code, 200)

    def test_single_language_comparison_detail_does_not_crash_on_alternates(self):
        # Beta 8.9 regression: localized_alternates() must not 500 while
        # building the hreflang for the *other*, untranslated language.
        make_comparison(slug="single-lang-alt", languages=("en",))
        resp = self.client.get("/en/compare/single-lang-alt-en/")
        self.assertEqual(resp.status_code, 200)

    def test_unpublished_comparison_is_public_404(self):
        c = Comparison.objects.create(status="draft", published_at=None)
        c.create_translation("en", title="Draft", intro="i", body="b", slug="draft-cmp-en")
        resp = self.client.get("/en/compare/draft-cmp-en/")
        self.assertEqual(resp.status_code, 404)


class ComparisonInventoryConsistencyTests(TestCase):
    def test_inventory_count_matches_list_queryset(self):
        from core.services import get_public_inventory
        from django.core.cache import cache

        cache.clear()
        make_comparison(slug="inv-en", languages=("en",))
        make_comparison(slug="inv-de", languages=("de",))
        make_comparison(slug="inv-both", languages=("en", "de"))

        for lang in ("en", "de"):
            data = get_public_inventory(lang)
            self.assertEqual(
                data["counts"]["comparisons"],
                Comparison.objects.visible_in_language(lang).count(),
            )
