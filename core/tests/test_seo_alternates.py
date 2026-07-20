"""
Beta 8.9: core.seo.utils.localized_alternates() must be robust against
missing Parler translations - never a 500, never an empty href, never a
wrong-language slug under the wrong prefix, only genuinely reachable
alternates, and the active Django language must be unchanged afterwards.
"""
from django.test import RequestFactory, TestCase
from django.utils import timezone
from django.utils.translation import get_language

from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin
from core.seo.utils import localized_alternates
from guides.models import Guide
from prompts.models import Prompt
from usecases.models import UseCase


def make(model, *, slug, languages, status=EditorialWorkflowMixin.STATUS_PUBLISHED,
         published_at=None, extra_translation_kwargs=None, **create_kwargs):
    if published_at is None and status == EditorialWorkflowMixin.STATUS_PUBLISHED:
        published_at = timezone.now()
    obj = model.objects.create(status=status, published_at=published_at, **create_kwargs)
    extra = extra_translation_kwargs or {}
    for lang in languages:
        obj.create_translation(
            lang, title=f"{model.__name__} {slug} {lang}", intro="i", body="b",
            slug=f"{slug}-{lang}", **extra,
        )
    return obj


class BilingualAlternatesTests(TestCase):
    def _alts_for(self, obj):
        request = RequestFactory().get("/en/whatever/")
        alts = localized_alternates(request, obj=obj)
        return {a.lang: a.url for a in alts}

    def test_prompt_has_both_language_alternates_with_correct_slugs(self):
        p = make(Prompt, slug="bilingual-prompt", languages=("en", "de"))
        alts = self._alts_for(p)
        self.assertIn("en", alts)
        self.assertIn("de", alts)
        self.assertTrue(alts["en"].endswith("/en/prompts/bilingual-prompt-en/"))
        self.assertTrue(alts["de"].endswith("/de/prompts/bilingual-prompt-de/"))

    def test_guide_has_both_language_alternates_with_correct_slugs(self):
        g = make(Guide, slug="bilingual-guide", languages=("en", "de"))
        alts = self._alts_for(g)
        self.assertTrue(alts["en"].endswith("/en/guides/bilingual-guide-en/"))
        self.assertTrue(alts["de"].endswith("/de/guides/bilingual-guide-de/"))

    def test_usecase_has_both_language_alternates_with_correct_slugs(self):
        u = make(UseCase, slug="bilingual-usecase", languages=("en", "de"),
                 extra_translation_kwargs={"persona": "Founder"})
        alts = self._alts_for(u)
        self.assertTrue(alts["en"].endswith("/en/usecases/bilingual-usecase-en/"))
        self.assertTrue(alts["de"].endswith("/de/usecases/bilingual-usecase-de/"))

    def test_comparison_has_both_language_alternates_with_correct_slugs(self):
        c = make(Comparison, slug="bilingual-comparison", languages=("en", "de"))
        alts = self._alts_for(c)
        self.assertTrue(alts["en"].endswith("/en/compare/bilingual-comparison-en/"))
        self.assertTrue(alts["de"].endswith("/de/compare/bilingual-comparison-de/"))

    def test_no_duplicate_language_entries(self):
        p = make(Prompt, slug="dup-check", languages=("en", "de"))
        request = RequestFactory().get("/en/whatever/")
        alts = localized_alternates(request, obj=p)
        langs = [a.lang for a in alts if a.lang != "x-default"]
        self.assertEqual(len(langs), len(set(langs)))


class EnglishOnlyAlternatesTests(TestCase):
    def _alts_for(self, obj):
        request = RequestFactory().get("/en/whatever/")
        return localized_alternates(request, obj=obj)

    def test_prompt_en_only_has_en_alt_and_no_de_alt(self):
        p = make(Prompt, slug="en-only-prompt", languages=("en",))
        alts = self._alts_for(p)
        langs = {a.lang for a in alts}
        self.assertIn("en", langs)
        self.assertNotIn("de", langs)

    def test_no_error_for_english_only_object(self):
        p = make(Prompt, slug="en-only-safe", languages=("en",))
        try:
            self._alts_for(p)
        except Exception as exc:  # noqa: BLE001 - explicit assertion, not a catch-all in prod code
            self.fail(f"localized_alternates() raised {exc!r} for an EN-only object")

    def test_comparison_en_only_has_en_alt_and_no_de_alt_no_crash(self):
        # Comparison.get_absolute_url() raises DoesNotExist (not TypeError)
        # for a missing translation - this is the concrete regression check.
        c = make(Comparison, slug="en-only-comparison", languages=("en",))
        alts = self._alts_for(c)
        langs = {a.lang for a in alts}
        self.assertIn("en", langs)
        self.assertNotIn("de", langs)

    def test_no_german_slug_leaks_under_english_alt(self):
        p = make(Prompt, slug="leak-check-en", languages=("en",))
        alts = self._alts_for(p)
        for a in alts:
            if a.lang == "en":
                self.assertIn("/en/", a.url)
                self.assertNotIn("/de/", a.url)


class GermanOnlyAlternatesTests(TestCase):
    def _alts_for(self, obj):
        request = RequestFactory().get("/de/whatever/")
        return localized_alternates(request, obj=obj)

    def test_prompt_de_only_has_de_alt_and_no_en_alt(self):
        p = make(Prompt, slug="de-only-prompt", languages=("de",))
        alts = self._alts_for(p)
        langs = {a.lang for a in alts}
        self.assertIn("de", langs)
        self.assertNotIn("en", langs)

    def test_comparison_de_only_has_de_alt_and_no_en_alt_no_crash(self):
        c = make(Comparison, slug="de-only-comparison", languages=("de",))
        alts = self._alts_for(c)
        langs = {a.lang for a in alts}
        self.assertIn("de", langs)
        self.assertNotIn("en", langs)

    def test_usecase_de_only_no_crash(self):
        # UseCase.get_absolute_url() raises NoReverseMatch (via reverse()
        # with slug=None), not DoesNotExist - a different exception class,
        # exercised here specifically.
        u = make(UseCase, slug="de-only-usecase", languages=("de",),
                 extra_translation_kwargs={"persona": ""})
        try:
            alts = self._alts_for(u)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"localized_alternates() raised {exc!r} for a DE-only UseCase")
        langs = {a.lang for a in alts}
        self.assertIn("de", langs)
        self.assertNotIn("en", langs)

    def test_no_english_slug_leaks_under_german_alt(self):
        p = make(Prompt, slug="leak-check-de", languages=("de",))
        alts = self._alts_for(p)
        for a in alts:
            if a.lang == "de":
                self.assertIn("/de/", a.url)
                self.assertNotIn("/en/", a.url)


class LanguageStateTests(TestCase):
    def test_active_language_unchanged_after_call(self):
        p = make(Prompt, slug="state-check", languages=("en", "de"))
        request = RequestFactory().get("/en/whatever/")
        before = get_language()
        localized_alternates(request, obj=p)
        after = get_language()
        self.assertEqual(before, after)

    def test_active_language_unchanged_even_for_english_only_object(self):
        p = make(Prompt, slug="state-check-2", languages=("en",))
        request = RequestFactory().get("/de/whatever/")
        before = get_language()
        localized_alternates(request, obj=p)
        after = get_language()
        self.assertEqual(before, after)


class CanonicalUnaffectedTests(TestCase):
    def test_prompt_detail_canonical_is_unaffected_by_alternates_fix(self):
        make(Prompt, slug="canonical-check", languages=("en",))
        resp = self.client.get("/en/prompts/canonical-check-en/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('rel="canonical"', html)
        self.assertIn("/en/prompts/canonical-check-en/", html)


class NoObjectFallbackUnaffectedTests(TestCase):
    """The url_name-only path (list/index pages without an obj) must keep
    working exactly as before - it is untouched by this slice's fix."""

    def test_list_page_alternates_still_use_url_name_fallback(self):
        request = RequestFactory().get("/en/prompts/")
        alts = localized_alternates(request, url_name="prompts:list")
        langs = {a.lang for a in alts}
        self.assertIn("en", langs)
        self.assertIn("de", langs)
