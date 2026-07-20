"""
Beta 8.9: core.templatetags.i18n_next.i18n_next() (the global navbar
language-switcher tag) must not crash the whole page when the current
detail object has no translation in the target language. This was a
previously-unknown, more severe sibling of the localized_alternates()
bug - it could 500 an entire Comparison/UseCase detail page render, not
just omit an SEO <head> alternate.

Beta 8.10: _translated_slug_from_parler() itself was found to silently
return a PARLER_LANGUAGES-fallback slug (not None) when the target language
has no translation, because safe_translation_getter() under switch_language()
falls back by default. Combined with lang_override(target_lang) in
_detail_url_for(), this produced a URL with the RIGHT language prefix but
the WRONG (fallback) language's slug - e.g. "/de/guides/<en-slug>/" - which
404s under any strict, language-aware detail view. Fixed generically (not
Guide-specific in the fix itself, though discovered via Guide work) by
guarding on has_translation(lang) first, matching the same pattern already
used in localized_alternates() since Beta 8.9.
"""
from django.template import Context
from django.test import RequestFactory, TestCase
from django.utils import timezone, translation

from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin
from core.templatetags.i18n_next import i18n_next
from guides.models import Guide
from usecases.models import UseCase


def make_comparison(*, slug, languages):
    c = Comparison.objects.create(
        status=EditorialWorkflowMixin.STATUS_PUBLISHED, published_at=timezone.now()
    )
    for lang in languages:
        c.create_translation(lang, title=f"Cmp {slug}", intro="i", body="b", slug=f"{slug}-{lang}")
    return c


def make_usecase(*, slug, languages):
    u = UseCase.objects.create(
        status=EditorialWorkflowMixin.STATUS_PUBLISHED, published_at=timezone.now(),
    )
    u.translations.all().delete()
    for lang in languages:
        u.create_translation(lang, title=f"Uc {slug}", intro="i", body="b", slug=f"{slug}-{lang}", persona="")
    return u


def make_guide(*, slug, languages):
    g = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_PUBLISHED, published_at=timezone.now())
    for lang in languages:
        g.create_translation(lang, title=f"Guide {slug}", intro="i", body="b", slug=f"{slug}-{lang}")
    return g


def _ctx(obj):
    request = RequestFactory().get("/en/whatever/")
    return Context({"request": request, "object": obj})


class I18nNextBilingualTests(TestCase):
    def test_comparison_switches_to_correct_german_slug(self):
        c = make_comparison(slug="switch-cmp", languages=("en", "de"))
        url = i18n_next(_ctx(c), "de")
        self.assertTrue(url.endswith("/de/compare/switch-cmp-de/"))

    def test_usecase_switches_to_correct_german_slug(self):
        u = make_usecase(slug="switch-uc", languages=("en", "de"))
        url = i18n_next(_ctx(u), "de")
        self.assertTrue(url.endswith("/de/usecases/switch-uc-de/"))

    def test_guide_switches_to_correct_german_slug(self):
        g = make_guide(slug="switch-guide", languages=("en", "de"))
        url = i18n_next(_ctx(g), "de")
        self.assertTrue(url.endswith("/de/guides/switch-guide-de/"))


class I18nNextGuideMissingTranslationTests(TestCase):
    """Beta 8.10: the bug that motivated this slice - reproduced directly
    against the shared i18n_next helper via Guide, confirmed fixed."""

    def test_en_only_guide_does_not_produce_wrong_language_slug_under_de(self):
        # Ambient language must match what a real /en/... request would
        # have (LocaleMiddleware activates it for the whole request-response
        # cycle) - a bare RequestFactory() request does not itself activate
        # anything, so this must be wrapped in override() to be a fair test.
        g = make_guide(slug="en-only-switch-guide", languages=("en",))
        with translation.override("en"):
            try:
                url = i18n_next(_ctx(g), "de")
            except Exception as exc:  # noqa: BLE001
                self.fail(f"i18n_next() raised {exc!r} for a guide with no DE translation")
        # Must never be the EN slug rendered under a /de/ prefix (the
        # confirmed pre-fix bug) - the fixed behavior stays on the guide's
        # own working (EN) URL instead.
        self.assertFalse(url.startswith("/de/guides/en-only-switch-guide"))
        self.assertTrue(url.endswith("/guides/en-only-switch-guide-en/"))

    def test_de_only_guide_does_not_produce_wrong_language_slug_under_en(self):
        g = make_guide(slug="de-only-switch-guide", languages=("de",))
        request = RequestFactory().get("/de/whatever/")
        with translation.override("de"):
            try:
                url = i18n_next(Context({"request": request, "object": g}), "en")
            except Exception as exc:  # noqa: BLE001
                self.fail(f"i18n_next() raised {exc!r} for a guide with no EN translation")
        # Must never be the DE slug rendered under an /en/ prefix.
        self.assertFalse(url.startswith("/en/guides/de-only-switch-guide"))

    def test_full_page_render_does_not_500_for_en_only_guide(self):
        make_guide(slug="page-render-check-guide", languages=("en",))
        resp = self.client.get("/en/guides/page-render-check-guide-en/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        # The "de" switcher option must not point at the EN slug under a
        # /de/ prefix (the confirmed pre-fix bug); staying on the current
        # working EN URL is the accepted fixed behavior.
        self.assertNotIn('data-next="/de/guides/page-render-check-guide-en/"', html)


class I18nNextMissingTranslationDoesNotCrashTests(TestCase):
    def test_comparison_en_only_no_crash_on_de_switch(self):
        # Beta 8.10 finding, fixed in Beta 8.11: Comparison.get_absolute_url()
        # used to call reverse() *inside* switch_language(self, lang) with a
        # plain `self.slug` attribute access - parler's use_fallback=True
        # descriptor silently substituted the EN translation's slug for a
        # missing "de" translation, and reverse() picked up the requested
        # "de" prefix, producing "/de/compare/<en-slug>/" - a target that
        # 404s under ComparisonDetailView's strict resolution. Fixed by
        # checking live_i18n directly and has_translation(lang) before ever
        # reading the translation, returning "#" (matching Guide/Prompt's
        # existing placeholder convention) instead of a broken cross-
        # language URL.
        c = make_comparison(slug="en-only-switch", languages=("en",))
        try:
            url = i18n_next(_ctx(c), "de")
        except Exception as exc:  # noqa: BLE001 - explicit assertion, not prod code
            self.fail(f"i18n_next() raised {exc!r} for a comparison with no DE translation")
        self.assertFalse(url.startswith("/de/"))
        self.assertEqual(url, "#")

    def test_usecase_en_only_no_crash_on_de_switch(self):
        # Beta 8.10: UseCase.get_absolute_url() calls reverse() *outside*
        # switch_language(self, lang) (unlike Comparison's), so Django's
        # ambient language has already been restored by the time reverse()
        # runs - the result stays on the current (EN) page instead of
        # producing a broken "/de/" target. Confirmed via the fixed
        # core.templatetags.i18n_next._translated_slug_from_parler() guard.
        u = make_usecase(slug="en-only-uc-switch", languages=("en",))
        try:
            url = i18n_next(_ctx(u), "de")
        except Exception as exc:  # noqa: BLE001
            self.fail(f"i18n_next() raised {exc!r} for a usecase with no DE translation")
        self.assertTrue(url.startswith("/en/"))
        self.assertNotIn("/de/", url)

    def test_full_page_render_does_not_500_for_en_only_comparison(self):
        make_comparison(slug="page-render-check", languages=("en",))
        resp = self.client.get("/en/compare/page-render-check-en/")
        self.assertEqual(resp.status_code, 200)
        # Beta 8.11: the "de" switcher option must be the safe placeholder,
        # never a broken "/de/compare/<en-slug>/" target (see Beta 8.11 fix
        # to Comparison.get_absolute_url()).
        self.assertIn('data-next="#"', resp.content.decode())
        self.assertNotIn('data-next="/de/', resp.content.decode())


class I18nNextNoRequestFallbackTests(TestCase):
    def test_missing_request_returns_root_without_crash(self):
        url = i18n_next(Context({}), "de")
        self.assertEqual(url, "/")


class I18nNextListPageFallbackTests(TestCase):
    def test_list_page_without_object_swaps_prefix(self):
        request = RequestFactory().get("/en/compare/")
        url = i18n_next(Context({"request": request}), "de")
        self.assertEqual(url, "/de/compare/")
