"""
Coverage-Schritt 2/3: behavioral + security regression tests for
core.views_i18n.set_language_smart, the custom "i18n/setlang/" endpoint used
by the language switcher.

History:
  - Coverage-Schritt 2 found two confirmed production defects here and
    documented them via @expectedFailure rather than pinning them as
    contract (see git history of this file for the original writeup):

    1. Open redirect (CWE-601, security): `next`/HTTP_REFERER were used as
       redirect targets without ever validating host/scheme via
       django.utils.http.url_has_allowed_host_and_scheme - unlike Django's
       own django.views.i18n.set_language.

    2. Glossar-Sprachwechsel (functional): _persist_language() activated the
       *target* language before resolve(next_path) ran. Django's
       i18n_patterns() resolver only matches a path against the *currently
       active* language (LocalePrefixPattern reads get_language()
       internally), so a `next` URL still carrying the *source* language's
       own prefix - the realistic case when a user switches away from the
       glossary page they are looking at - raised Resolver404, silently
       skipping the intended cross-language glossary slug/translation_group
       lookup.

  - Both are now fixed in core/views_i18n.py:
    1. `_resolve_safe_next_url()` validates `next`, then HTTP_REFERER, then
       falls back to "/" - mirroring django.views.i18n.set_language's own
       security contract - unconditionally, before any language validation.
    2. `_source_language_from_path()` extracts the *source* language from
       the next path's own i18n_patterns() prefix and resolve() runs under
       `translation.override(source_lang)`, independent of the ambient
       active language and before the target language is ever persisted.

  All tests below are normal, green regression tests - no
  @unittest.expectedFailure/xfail remain in this module.
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from glossary.models import GlossaryTerm

User = get_user_model()


class LanguageIsolationMixin:
    """Deterministic active-language setup/teardown (see Beta 10 Phase 7
    guidance: translation.deactivate_all() can leave the active language as
    None and break later Parler-dependent tests in the same process)."""

    def setUp(self):
        super().setUp()
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)


class SetLanguageMethodTests(LanguageIsolationMixin, TestCase):
    def test_get_is_not_allowed(self):
        resp = self.client.get(reverse("set_language"))
        self.assertEqual(resp.status_code, 405)


class SetLanguagePersistenceTests(LanguageIsolationMixin, TestCase):
    def test_switch_en_to_de_persists_session_and_cookie(self):
        resp = self.client.post(reverse("set_language"), {"language": "de", "next": "/de/"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.client.session[settings.LANGUAGE_COOKIE_NAME], "de")
        self.assertEqual(resp.cookies[settings.LANGUAGE_COOKIE_NAME].value, "de")

    def test_switch_de_to_en(self):
        resp = self.client.post(reverse("set_language"), {"language": "en", "next": "/en/"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.client.session[settings.LANGUAGE_COOKIE_NAME], "en")
        self.assertEqual(resp.cookies[settings.LANGUAGE_COOKIE_NAME].value, "en")

    def test_switching_to_the_already_active_language_still_persists(self):
        self.client.post(reverse("set_language"), {"language": "en", "next": "/en/"})
        resp = self.client.post(reverse("set_language"), {"language": "en", "next": "/en/"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.client.session[settings.LANGUAGE_COOKIE_NAME], "en")

    def test_missing_language_does_not_persist_anything(self):
        resp = self.client.post(reverse("set_language"), {"next": "/en/"})
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn(settings.LANGUAGE_COOKIE_NAME, self.client.session)
        self.assertNotIn(settings.LANGUAGE_COOKIE_NAME, resp.cookies)

    def test_empty_language_value_behaves_like_missing(self):
        resp = self.client.post(reverse("set_language"), {"language": "", "next": "/en/"})
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn(settings.LANGUAGE_COOKIE_NAME, self.client.session)

    def test_unsupported_language_code_does_not_persist(self):
        resp = self.client.post(reverse("set_language"), {"language": "not-a-lang", "next": "/en/"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, "/en/")
        self.assertNotIn(settings.LANGUAGE_COOKIE_NAME, self.client.session)
        self.assertNotIn(settings.LANGUAGE_COOKIE_NAME, resp.cookies)


class SetLanguageRedirectTargetTests(LanguageIsolationMixin, TestCase):
    def test_missing_next_and_referer_falls_back_to_root(self):
        resp = self.client.post(reverse("set_language"), {"language": "en"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, "/")

    def test_referer_used_when_next_is_missing(self):
        resp = self.client.post(
            reverse("set_language"),
            {"language": "en"},
            HTTP_REFERER="/en/guides/",
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, "/en/guides/")

    def test_next_with_querystring_is_preserved_for_a_non_glossary_route(self):
        resp = self.client.post(
            reverse("set_language"),
            {"language": "en", "next": "/en/guides/?ref=newsletter"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, "/en/guides/?ref=newsletter")

    def test_next_with_fragment_is_preserved_for_a_non_glossary_route(self):
        resp = self.client.post(
            reverse("set_language"),
            {"language": "en", "next": "/en/guides/#section-2"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, "/en/guides/#section-2")

    def test_unresolvable_next_path_still_redirects_and_sets_cookie(self):
        resp = self.client.post(
            reverse("set_language"),
            {"language": "de", "next": "/this-route-does-not-exist-xyz/"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, "/this-route-does-not-exist-xyz/")
        self.assertEqual(resp.cookies[settings.LANGUAGE_COOKIE_NAME].value, "de")

    def test_fully_qualified_same_host_next_is_allowed(self):
        # url_has_allowed_host_and_scheme() explicitly allows a fully-
        # qualified URL back to the *same* host/scheme - this is not an
        # open redirect since the host matches request.get_host().
        resp = self.client.post(
            reverse("set_language"),
            {"language": "en", "next": "http://testserver/en/guides/"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, "http://testserver/en/guides/")


class SetLanguageGlossaryRedirectTests(LanguageIsolationMixin, TestCase):
    """The set_language_smart glossary special-case: same slug in target
    language, else translation_group fallback, else glossary:list.

    Fixed real-behavior note: resolve() now runs under
    translation.override(<source language extracted from next's own URL
    prefix>), independent of the target language being persisted - so both
    a `next` already prefixed with the target language *and* the realistic
    case of a `next` still carrying the pre-switch source language's prefix
    correctly reach the cross-language glossary lookup (see
    test_switching_away_from_a_source_language_glossary_page_cross_language_redirects
    below, formerly an expectedFailure documenting this exact defect)."""

    @classmethod
    def setUpTestData(cls):
        cls.same_slug_en = GlossaryTerm.objects.create(
            term="Prompt Engineering", slug="prompt-engineering", language="en",
            short_definition="EN def.",
        )
        cls.same_slug_de = GlossaryTerm.objects.create(
            term="Prompt Engineering", slug="prompt-engineering", language="de",
            short_definition="DE def.",
            translation_group=cls.same_slug_en.translation_group,
        )

        cls.group_en = GlossaryTerm.objects.create(
            term="Token", slug="token-en", language="en", short_definition="EN def.",
        )
        cls.group_de = GlossaryTerm.objects.create(
            term="Token", slug="token-de", language="de", short_definition="DE def.",
            translation_group=cls.group_en.translation_group,
        )

        cls.orphan_en = GlossaryTerm.objects.create(
            term="Solo Term", slug="solo-term-en", language="en", short_definition="EN only.",
        )

    def test_same_slug_in_target_language_wins(self):
        resp = self.client.post(
            reverse("set_language"),
            {"language": "de", "next": f"/de/glossary/{self.same_slug_en.slug}/"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, f"/de/glossary/{self.same_slug_de.slug}/")

    def test_translation_group_fallback_used_when_slug_differs(self):
        # The literal slug in `next` ("token-en") does not exist as a
        # language="de" row, forcing the secondary translation_group
        # lookup - a real client can send any slug string in `next`
        # regardless of what the site itself would generate.
        resp = self.client.post(
            reverse("set_language"),
            {"language": "de", "next": f"/de/glossary/{self.group_en.slug}/"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, f"/de/glossary/{self.group_de.slug}/")

    def test_no_translation_falls_back_to_glossary_list(self):
        resp = self.client.post(
            reverse("set_language"),
            {"language": "de", "next": f"/de/glossary/{self.orphan_en.slug}/"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, "/de/glossary/")

    def test_slug_matching_no_term_at_all_falls_back_to_glossary_list(self):
        # An old/bookmarked URL referencing a slug that no longer exists in
        # any language at all (e.g. a deleted term) - distinct from
        # test_no_translation_falls_back_to_glossary_list, where the slug
        # does exist (just without a translation_group partner). No server
        # error either way.
        resp = self.client.post(
            reverse("set_language"),
            {"language": "de", "next": "/de/glossary/totally-made-up-slug-xyz/"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, "/de/glossary/")

    def test_glossary_match_sets_language_cookie(self):
        resp = self.client.post(
            reverse("set_language"),
            {"language": "de", "next": f"/de/glossary/{self.same_slug_en.slug}/"},
        )
        self.assertEqual(resp.cookies[settings.LANGUAGE_COOKIE_NAME].value, "de")

    def test_switching_away_from_a_source_language_glossary_page_cross_language_redirects(self):
        # The realistic case: `next` carries the *pre-switch* ("en") prefix,
        # exactly what a "switch to German" link on an English glossary
        # detail page sends. Formerly an expectedFailure (Resolver404 under
        # the already-activated target language); now fixed via
        # _source_language_from_path()/translation.override(source_lang).
        resp = self.client.post(
            reverse("set_language"),
            {"language": "de", "next": f"/en/glossary/{self.same_slug_en.slug}/"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, f"/de/glossary/{self.same_slug_de.slug}/")

    def test_switching_from_german_glossary_page_to_english_cross_language_redirects(self):
        # Same fix, opposite direction: next carries the "de" source prefix
        # while switching to "en".
        resp = self.client.post(
            reverse("set_language"),
            {"language": "en", "next": f"/de/glossary/{self.same_slug_de.slug}/"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, f"/en/glossary/{self.same_slug_en.slug}/")

    def test_switching_to_already_active_language_from_its_own_glossary_page_is_stable(self):
        # "Switching" to the language already active while looking at that
        # language's own term still resolves to itself, not an error.
        resp = self.client.post(
            reverse("set_language"),
            {"language": "en", "next": f"/en/glossary/{self.same_slug_en.slug}/"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, f"/en/glossary/{self.same_slug_en.slug}/")

    def test_querystring_and_fragment_are_not_carried_over_on_a_glossary_match(self):
        # Documented real contract (unchanged by the fix): a glossary match
        # builds a *fresh* target.get_absolute_url() rather than reusing
        # next_url, so any querystring/fragment on the original next is not
        # preserved - unlike the non-glossary passthrough branch tested in
        # SetLanguageRedirectTargetTests.
        resp = self.client.post(
            reverse("set_language"),
            {"language": "de", "next": f"/en/glossary/{self.same_slug_en.slug}/?ref=x#y"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, f"/de/glossary/{self.same_slug_de.slug}/")

    def test_note_glossaryterm_has_no_draft_concept(self):
        # GlossaryTerm (glossary/models.py) has no status/draft field at
        # all - every row is public by definition (GlossaryDetailView's own
        # get_queryset() only ever filters by language). "Draft-slug-leak"
        # is therefore not a real, testable contract for this model; this
        # test only documents that fact so it isn't silently assumed.
        self.assertFalse(hasattr(GlossaryTerm, "status"))


class SetLanguageHeaderInjectionSafetyTests(LanguageIsolationMixin, TestCase):
    """Two independent protection layers now cover CRLF/control-character
    attempts in `next`: url_has_allowed_host_and_scheme() rejects a control
    character only at *index 0* of the URL, so a CRLF later in the string
    still reaches HttpResponseRedirect - but Django's own HttpResponseRedirect
    percent-encodes CRLF via iri_to_uri() before it ever reaches the raw
    response header, independent of set_language_smart's own validation."""

    def test_crlf_in_next_is_percent_encoded_not_injected_into_headers(self):
        malicious_next = "/en/\r\nX-Injected: 1"
        resp = self.client.post(
            reverse("set_language"),
            {"language": "en", "next": malicious_next},
        )
        self.assertEqual(resp.status_code, 302)
        location = resp.headers["Location"]
        self.assertNotIn("\r", location)
        self.assertNotIn("\n", location)
        self.assertNotIn("X-Injected", resp.headers)

    def test_control_character_at_start_of_next_is_rejected(self):
        # unicodedata control-character check in
        # url_has_allowed_host_and_scheme() catches this case directly -
        # falls back to "/" since there's no safe referer either.
        resp = self.client.post(
            reverse("set_language"),
            {"language": "en", "next": "\x00/en/guides/"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, "/")


class SetLanguageSecurityTests(LanguageIsolationMixin, TestCase):
    """Pflicht-Securitytests: set_language_smart must never redirect
    off-site, regardless of whether the requested language is valid, and
    regardless of the specific bypass technique attempted in `next` or
    HTTP_REFERER. All assert real, now-fixed behavior - no expectedFailure."""

    def test_invalid_language_with_external_next_falls_back_safely(self):
        # "not-a-lang" is confirmed rejected by Django's own
        # check_for_language() (unlike e.g. "xx", which - somewhat
        # surprisingly - passes it); see SetLanguagePersistenceTests.
        # test_unsupported_language_code_does_not_persist for the same
        # fixture choice.
        resp = self.client.post(
            reverse("set_language"),
            {"language": "not-a-lang", "next": "https://evil.example/de/"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, "/")
        self.assertNotIn("evil.example", resp.url)
        self.assertNotIn(settings.LANGUAGE_COOKIE_NAME, self.client.session)
        self.assertNotIn(settings.LANGUAGE_COOKIE_NAME, resp.cookies)

    def test_valid_language_with_locally_resolvable_external_next_falls_back_safely(self):
        # The historical bug: next's *path* ("/de/") resolves locally, but
        # the full external URL used to survive into the final redirect
        # unchanged. Now rejected outright by url_has_allowed_host_and_scheme
        # before any path resolution happens.
        resp = self.client.post(
            reverse("set_language"),
            {"language": "de", "next": "https://evil.example/de/"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("evil.example", resp.url)
        self.assertEqual(resp.url, "/")
        # The requested language itself is valid and independent of the
        # rejected `next` - it is still persisted, matching the view's
        # real, unchanged contract of always honoring a valid `language`.
        self.assertEqual(self.client.session[settings.LANGUAGE_COOKIE_NAME], "de")
        self.assertEqual(resp.cookies[settings.LANGUAGE_COOKIE_NAME].value, "de")

    def test_protocol_relative_next_is_rejected(self):
        resp = self.client.post(
            reverse("set_language"),
            {"language": "de", "next": "//evil.example/de/"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("evil.example", resp.url)
        self.assertEqual(resp.url, "/")

    def test_userinfo_trick_next_is_rejected(self):
        resp = self.client.post(
            reverse("set_language"),
            {"language": "de", "next": "https://trusted.example@evil.example/de/"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("evil.example", resp.url)
        self.assertEqual(resp.url, "/")

    def test_backslash_variant_next_is_rejected(self):
        resp = self.client.post(
            reverse("set_language"),
            {"language": "de", "next": "/\\evil.example/de/"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("evil.example", resp.url)
        self.assertEqual(resp.url, "/")

    def test_pre_encoded_external_url_is_rejected(self):
        # Django decodes standard form-encoded POST bodies before
        # request.POST.get("next") ever sees the value, so a percent-encoded
        # external URL arrives as a plain external URL string - confirming
        # no bypass exists via this transport-level encoding.
        resp = self.client.post(
            reverse("set_language"),
            {"language": "de", "next": "https://evil.example/%2e%2e/de/"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("evil.example", resp.url)
        self.assertEqual(resp.url, "/")

    def test_external_http_referer_is_rejected_when_next_is_missing(self):
        resp = self.client.post(
            reverse("set_language"),
            {"language": "de"},
            HTTP_REFERER="http://evil.example/",
        )
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("evil.example", resp.url)
        self.assertEqual(resp.url, "/")

    def test_unsafe_next_falls_back_to_a_safe_internal_referer(self):
        resp = self.client.post(
            reverse("set_language"),
            {"language": "en", "next": "https://evil.example/"},
            HTTP_REFERER="/en/guides/",
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, "/en/guides/")

    def test_unsafe_next_and_unsafe_referer_falls_back_to_root(self):
        resp = self.client.post(
            reverse("set_language"),
            {"language": "en", "next": "https://evil.example/"},
            HTTP_REFERER="http://also-evil.example/",
        )
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("evil.example", resp.url)
        self.assertEqual(resp.url, "/")

    def test_https_request_rejects_an_insecure_http_target_on_the_same_host(self):
        resp = self.client.post(
            reverse("set_language"),
            {"language": "en", "next": "http://testserver/en/guides/"},
            secure=True,
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, "/")

    def test_crlf_control_character_attempt_in_next_never_redirects_off_site(self):
        resp = self.client.post(
            reverse("set_language"),
            {"language": "de", "next": "https://evil.example/\r\nSet-Cookie: x=1"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("evil.example", resp.url)
        self.assertEqual(resp.url, "/")

    def test_no_technical_detail_leak_on_rejected_next(self):
        resp = self.client.post(
            reverse("set_language"),
            {"language": "de", "next": "https://evil.example/de/"},
        )
        body = resp.content.decode()
        self.assertNotIn("Traceback", body)
        self.assertNotIn("evil.example", body)
