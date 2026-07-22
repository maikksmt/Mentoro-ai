"""
Beta 11.6: unit coverage for the shared preview primitives in
``core/editorial_preview.py``, isolated from any admin view, URL or
template. The behavioral guarantees these primitives serve (fail-closed
language, 404-not-403, noindex/nofollow, uncacheable) are already exercised
end-to-end by ``guides/tests/test_draft_preview_*.py`` and
``prompts/tests/test_draft_preview_*.py`` - this module only proves the
extracted functions themselves are correct in isolation.
"""
from django.http import HttpResponse
from django.test import TestCase, override_settings
from django.utils.cache import add_never_cache_headers

from core.editorial_preview import (
    PREVIEW_ROBOTS,
    apply_editorial_preview_headers,
    build_preview_seo_meta,
    has_saved_translation,
    is_supported_preview_language,
)
from core.seo.types import SeoMeta
from prompts.models import Prompt


def _cache_control_directives(response) -> set[str]:
    """Robust token parse of Cache-Control, ignoring directive ordering."""
    raw = response.get("Cache-Control", "")
    return {token.strip() for token in raw.split(",") if token.strip()}


class HasSavedTranslationTests(TestCase):
    def test_true_for_a_language_with_a_stored_translation_row(self):
        prompt = Prompt.objects.create()
        prompt.create_translation("en", title="T", intro="i", body="b", outro="o", slug="hst-en")
        self.assertTrue(has_saved_translation(prompt, "en"))

    def test_false_for_a_language_never_saved(self):
        prompt = Prompt.objects.create()
        prompt.create_translation("en", title="T", intro="i", body="b", outro="o", slug="hst-en-only")
        self.assertFalse(has_saved_translation(prompt, "de"))

    def test_false_for_an_in_memory_only_initialized_translation(self):
        """The Beta 11.4-confirmed Parler admin quirk: initializing a
        language tab must not count as saved, even though Parler's own
        ``has_translation()`` reports True for it."""
        prompt = Prompt.objects.create()
        prompt.create_translation("en", title="T", intro="i", body="b", outro="o", slug="hst-init")
        prompt.set_current_language("de", initialize=True)
        self.assertTrue(prompt.has_translation("de"))  # the misleading signal
        self.assertFalse(has_saved_translation(prompt, "de"))


class IsSupportedPreviewLanguageTests(TestCase):
    def test_true_for_every_configured_language(self):
        self.assertTrue(is_supported_preview_language("en"))
        self.assertTrue(is_supported_preview_language("de"))

    def test_false_for_an_unconfigured_language(self):
        self.assertFalse(is_supported_preview_language("fr"))

    def test_false_for_empty_or_garbage_input(self):
        self.assertFalse(is_supported_preview_language(""))
        self.assertFalse(is_supported_preview_language("not-a-language"))

    @override_settings(LANGUAGES=(("en", "English"),))
    def test_reflects_the_current_settings_languages(self):
        self.assertTrue(is_supported_preview_language("en"))
        self.assertFalse(is_supported_preview_language("de"))


class ApplyEditorialPreviewHeadersTests(TestCase):
    """
    Beta 11.6 hardening: the helper must guarantee the full Cache-Control
    contract itself (private, no-store, no-cache, must-revalidate,
    max-age=0), not rely solely on the calling view being wrapped in
    ``admin_site.admin_view()``/``never_cache``.
    """

    REQUIRED_DIRECTIVES = {"private", "no-store", "no-cache", "must-revalidate", "max-age=0"}

    def test_sets_robots_pragma_and_content_language(self):
        response = apply_editorial_preview_headers(HttpResponse("ok"), "de")
        self.assertEqual(response["X-Robots-Tag"], PREVIEW_ROBOTS)
        self.assertEqual(response["Pragma"], "no-cache")
        self.assertEqual(response["Content-Language"], "de")

    def test_returns_the_same_response_instance(self):
        response = HttpResponse("ok")
        self.assertIs(apply_editorial_preview_headers(response, "en"), response)

    def test_cache_control_contains_the_full_contract(self):
        response = apply_editorial_preview_headers(HttpResponse("ok"), "en")
        directives = _cache_control_directives(response)
        self.assertTrue(
            self.REQUIRED_DIRECTIVES.issubset(directives),
            f"missing {self.REQUIRED_DIRECTIVES - directives} in {directives}",
        )

    def test_cache_control_never_contains_public(self):
        response = apply_editorial_preview_headers(HttpResponse("ok"), "en")
        self.assertNotIn("public", _cache_control_directives(response))

    def test_body_status_and_content_type_are_unchanged(self):
        response = HttpResponse("draft body", status=200, content_type="text/html; charset=utf-8")
        apply_editorial_preview_headers(response, "en")
        self.assertEqual(response.content, b"draft body")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/html; charset=utf-8")

    def test_unrelated_header_and_cookie_are_unchanged(self):
        response = HttpResponse("ok")
        response["X-Custom-Marker"] = "keep-me"
        response.set_cookie("sessionid", "abc123")
        apply_editorial_preview_headers(response, "en")
        self.assertEqual(response["X-Custom-Marker"], "keep-me")
        self.assertEqual(response.cookies["sessionid"].value, "abc123")

    def test_applying_the_helper_twice_keeps_the_contract_and_no_public(self):
        response = HttpResponse("ok")
        apply_editorial_preview_headers(response, "en")
        apply_editorial_preview_headers(response, "en")
        directives = _cache_control_directives(response)
        self.assertTrue(self.REQUIRED_DIRECTIVES.issubset(directives))
        self.assertNotIn("public", directives)

    def test_helper_then_never_cache_keeps_the_full_contract(self):
        response = HttpResponse("ok")
        apply_editorial_preview_headers(response, "en")
        add_never_cache_headers(response)
        directives = _cache_control_directives(response)
        self.assertTrue(self.REQUIRED_DIRECTIVES.issubset(directives))
        self.assertNotIn("public", directives)

    def test_never_cache_then_helper_keeps_the_full_contract(self):
        response = HttpResponse("ok")
        add_never_cache_headers(response)
        apply_editorial_preview_headers(response, "en")
        directives = _cache_control_directives(response)
        self.assertTrue(self.REQUIRED_DIRECTIVES.issubset(directives))
        self.assertNotIn("public", directives)

    def test_helper_alone_without_never_cache_still_gives_the_full_contract(self):
        """The behavior this hardening exists for: never_cache must not be
        the only source of the Cache-Control contract."""
        response = apply_editorial_preview_headers(HttpResponse("ok"), "en")
        self.assertTrue(self.REQUIRED_DIRECTIVES.issubset(_cache_control_directives(response)))


class BuildPreviewSeoMetaTests(TestCase):
    def test_returns_a_seo_meta_with_the_given_title_and_description(self):
        seo = build_preview_seo_meta(title="T", description="D")
        self.assertIsInstance(seo, SeoMeta)
        self.assertEqual(seo.title, "T")
        self.assertEqual(seo.description, "D")

    def test_robots_is_fixed_to_noindex_nofollow(self):
        seo = build_preview_seo_meta(title="T", description="D")
        self.assertEqual(seo.robots, PREVIEW_ROBOTS)

    def test_carries_no_canonical_alternates_or_json_ld(self):
        seo = build_preview_seo_meta(title="T", description="D")
        self.assertEqual(seo.canonical, "")
        self.assertEqual(seo.alternates, [])
        self.assertIsNone(seo.json_ld)
        self.assertIsNone(seo.og_image)

    def test_two_calls_do_not_share_a_mutable_alternates_list(self):
        first = build_preview_seo_meta(title="A", description="a")
        second = build_preview_seo_meta(title="B", description="b")
        first.alternates.append("marker")
        self.assertEqual(second.alternates, [])
