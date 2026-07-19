"""
Beta 8.9: core.templatetags.i18n_next.i18n_next() (the global navbar
language-switcher tag) must not crash the whole page when the current
detail object has no translation in the target language. This was a
previously-unknown, more severe sibling of the localized_alternates()
bug - it could 500 an entire Comparison/UseCase detail page render, not
just omit an SEO <head> alternate.
"""
from django.template import Context
from django.test import RequestFactory, TestCase
from django.utils import timezone

from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin
from core.templatetags.i18n_next import i18n_next
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


class I18nNextMissingTranslationDoesNotCrashTests(TestCase):
    def test_comparison_en_only_no_crash_on_de_switch(self):
        c = make_comparison(slug="en-only-switch", languages=("en",))
        try:
            url = i18n_next(_ctx(c), "de")
        except Exception as exc:  # noqa: BLE001 - explicit assertion, not prod code
            self.fail(f"i18n_next() raised {exc!r} for a comparison with no DE translation")
        self.assertTrue(url.startswith("/de/"))

    def test_usecase_en_only_no_crash_on_de_switch(self):
        u = make_usecase(slug="en-only-uc-switch", languages=("en",))
        try:
            url = i18n_next(_ctx(u), "de")
        except Exception as exc:  # noqa: BLE001
            self.fail(f"i18n_next() raised {exc!r} for a usecase with no DE translation")
        self.assertTrue(url.startswith("/de/"))

    def test_full_page_render_does_not_500_for_en_only_comparison(self):
        make_comparison(slug="page-render-check", languages=("en",))
        resp = self.client.get("/en/compare/page-render-check-en/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('data-next="/de/', resp.content.decode())


class I18nNextNoRequestFallbackTests(TestCase):
    def test_missing_request_returns_root_without_crash(self):
        url = i18n_next(Context({}), "de")
        self.assertEqual(url, "/")


class I18nNextListPageFallbackTests(TestCase):
    def test_list_page_without_object_swaps_prefix(self):
        request = RequestFactory().get("/en/compare/")
        url = i18n_next(Context({"request": request}), "de")
        self.assertEqual(url, "/de/compare/")
