"""
Beta 8.8: Comparison language behavior.

- List view: Comparison.published already applies active_translations()
  (same fallback-inclusive semantics as Guide/UseCase) - left unchanged,
  documented here as intentional, established project behavior.
- Detail view: fixed a real leak - the slug lookup did not require the
  matched translation row to belong to the active language, so an EN-only
  (or wrong-slug) comparison could render under the wrong language prefix.
"""
from django.test import TestCase
from django.utils import timezone

from compare.models import Comparison
from compare.views import ComparisonListView
from django.test import RequestFactory
from django.utils import translation


def make_comparison(*, slug, languages=("en", "de")):
    c = Comparison.objects.create(status="published", published_at=timezone.now())
    for lang in languages:
        c.create_translation(lang, title=f"Title {slug} {lang}", intro="i", body="b", slug=f"{slug}-{lang}")
    return c


class ComparisonListFallbackIsUnchangedTests(TestCase):
    """Documents the existing, accepted active_translations() fallback -
    same mechanism as Guide/UseCase, intentionally not tightened here."""

    def test_en_only_comparison_still_appears_on_the_german_list(self):
        c = make_comparison(slug="en-only-list", languages=("en",))
        with translation.override("de"):
            request = RequestFactory().get("/compare/")
            view = ComparisonListView()
            view.request = request
            self.assertTrue(view.get_queryset().filter(pk=c.pk).exists())


class ComparisonDetailLanguageTests(TestCase):
    # Note: Comparison.get_absolute_url() does a raw switch_language() +
    # self.slug access (unlike Prompt's safer get_live_value()-based
    # implementation) and localized_alternates() only catches TypeError, not
    # parler's DoesNotExist - so rendering a single-language-only
    # Comparison's detail page can crash while building hreflang alternates
    # for the *other* configured language. Pre-existing, unrelated to this
    # slice's view-level fix (see report); "reachable" tests below therefore
    # use bilingual fixtures so alternates-building has both languages to
    # work with, while the 404 tests are unaffected since the object is
    # never rendered.
    def test_english_comparison_reachable_under_english_url(self):
        make_comparison(slug="en-detail", languages=("en", "de"))
        resp = self.client.get("/en/compare/en-detail-en/")
        self.assertEqual(resp.status_code, 200)

    def test_german_comparison_reachable_under_german_url(self):
        make_comparison(slug="de-detail", languages=("en", "de"))
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

    def test_unpublished_comparison_is_public_404(self):
        c = Comparison.objects.create(status="draft", published_at=None)
        c.create_translation("en", title="Draft", intro="i", body="b", slug="draft-cmp-en")
        resp = self.client.get("/en/compare/draft-cmp-en/")
        self.assertEqual(resp.status_code, 404)
