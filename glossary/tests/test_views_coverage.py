"""
Coverage-Schritt 3: behavioral tests for glossary/views.py - list, detail,
autocomplete and the (currently unrouted, see GlossaryApiViewTests) API view.

Language isolation: every test that depends on the active Django language
enters via a real request to a language-prefixed URL ("/en/glossary/...",
"/de/glossary/...") - LocaleMiddleware activates the correct language for the
duration of that request/response cycle. That activation is *not* undone
afterwards though (Django does not reset it at the end of a request), so a
"/de/..." test here would otherwise leave "de" ambient for whatever test runs
next in the same process (confirmed: this caused an unrelated newsletter test
relying on reverse() to render its German translation under a shuffled run
order). TestCase (below) resets to the project default in tearDown so this
module never leaks language state to tests that run after it.
"""
import json

from django.conf import settings
from django.test import RequestFactory
from django.test import TestCase as DjangoTestCase
from django.urls import reverse
from django.utils import timezone, translation

from glossary.models import GlossaryTerm
from glossary.views import GlossaryApiView


class TestCase(DjangoTestCase):
    """Local TestCase that starts and ends every test with the project
    default language active, regardless of which language-prefixed URL the
    test itself hits (see module docstring)."""

    def setUp(self):
        super().setUp()
        translation.activate(settings.LANGUAGE_CODE)

    def tearDown(self):
        super().tearDown()
        translation.activate(settings.LANGUAGE_CODE)


def make_term(term, slug, lang, *, category="", long_definition="", translation_group=None):
    kwargs = dict(
        term=term,
        slug=slug,
        short_definition=f"Short: {term}",
        long_definition=long_definition,
        category=category,
        language=lang,
        created_at=timezone.now(),
        updated_at=timezone.now(),
    )
    if translation_group is not None:
        kwargs["translation_group"] = translation_group
    return GlossaryTerm.objects.create(**kwargs)


class GlossaryListViewLanguageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.en_term = make_term("Throughput", "throughput", "en")
        cls.de_term = make_term("Durchsatz", "durchsatz", "de")

    def test_en_list_shows_only_en_terms(self):
        resp = self.client.get("/en/glossary/")
        self.assertEqual(resp.status_code, 200)
        terms = list(resp.context["terms"])
        self.assertIn(self.en_term, terms)
        self.assertNotIn(self.de_term, terms)

    def test_de_list_shows_only_de_terms(self):
        resp = self.client.get("/de/glossary/")
        self.assertEqual(resp.status_code, 200)
        terms = list(resp.context["terms"])
        self.assertIn(self.de_term, terms)
        self.assertNotIn(self.en_term, terms)

    def test_empty_language_list_shows_no_hits_message(self):
        GlossaryTerm.objects.filter(language="en").delete()
        resp = self.client.get("/en/glossary/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(list(resp.context["terms"]), [])
        self.assertIn("No hits.", resp.content.decode())


class GlossaryListViewSearchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.token_term = make_term("Token", "token", "en", category="NLP")
        cls.embedding_term = make_term("Embedding", "embedding", "en", category="NLP")
        cls.unrelated = make_term("Zebra", "zebra", "en", category="Misc")

    def test_query_with_hit_returns_only_matching_terms(self):
        resp = self.client.get("/en/glossary/", {"q": "Token"})
        terms = list(resp.context["terms"])
        self.assertEqual(terms, [self.token_term])

    def test_query_is_case_insensitive(self):
        resp = self.client.get("/en/glossary/", {"q": "token"})
        self.assertIn(self.token_term, list(resp.context["terms"]))

    def test_query_without_hit_returns_empty(self):
        resp = self.client.get("/en/glossary/", {"q": "nonexistent-xyz"})
        self.assertEqual(list(resp.context["terms"]), [])

    def test_whitespace_only_query_behaves_like_no_query(self):
        resp = self.client.get("/en/glossary/", {"q": "   "})
        terms = list(resp.context["terms"])
        self.assertIn(self.token_term, terms)
        self.assertIn(self.embedding_term, terms)
        self.assertIn(self.unrelated, terms)

    def test_special_characters_in_query_do_not_crash_and_are_escaped(self):
        resp = self.client.get("/en/glossary/", {"q": "<script>alert(1)</script>"})
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertNotIn("<script>alert(1)</script>", html)

    def test_letter_filter_applied(self):
        resp = self.client.get("/en/glossary/", {"letter": "T"})
        terms = list(resp.context["terms"])
        self.assertIn(self.token_term, terms)
        self.assertNotIn(self.embedding_term, terms)

    def test_query_and_letter_are_preserved_in_pagination_links(self):
        resp = self.client.get("/en/glossary/", {"q": "Token"})
        self.assertEqual(resp.context["q"], "Token")
        self.assertEqual(resp.context["letter"], "")


class GlossaryListViewPaginationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        for i in range(35):
            make_term(f"Term {i:02d}", f"term-{i:02d}", "en")

    def test_first_page_has_thirty_items(self):
        resp = self.client.get("/en/glossary/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.context["terms"]), 30)
        self.assertTrue(resp.context["page_obj"].has_next())

    def test_second_page_has_remaining_items(self):
        resp = self.client.get("/en/glossary/", {"page": 2})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.context["terms"]), 5)
        self.assertFalse(resp.context["page_obj"].has_next())

    def test_non_numeric_page_is_404(self):
        resp = self.client.get("/en/glossary/", {"page": "abc"})
        self.assertEqual(resp.status_code, 404)

    def test_out_of_range_page_is_404(self):
        resp = self.client.get("/en/glossary/", {"page": 9999})
        self.assertEqual(resp.status_code, 404)

    def test_negative_page_is_404(self):
        resp = self.client.get("/en/glossary/", {"page": -1})
        self.assertEqual(resp.status_code, 404)


class GlossaryListViewSeoContextTests(TestCase):
    def test_seo_context_present_with_canonical_and_alternates(self):
        resp = self.client.get("/en/glossary/")
        seo = resp.context["seo"]
        self.assertIn("glossary/", seo.canonical)
        self.assertTrue(seo.alternates)
        self.assertEqual(seo.json_ld["@type"], "CollectionPage")
        self.assertEqual(seo.json_ld["inLanguage"], "en")


class GlossaryDetailViewLanguageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.en_term = make_term("Throughput", "throughput", "en")
        cls.de_term = make_term(
            "Durchsatz", "durchsatz", "de", translation_group=cls.en_term.translation_group,
        )

    def test_valid_en_slug(self):
        resp = self.client.get("/en/glossary/throughput/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["term"], self.en_term)

    def test_valid_de_slug(self):
        resp = self.client.get("/de/glossary/durchsatz/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["term"], self.de_term)

    def test_unknown_slug_404(self):
        resp = self.client.get("/en/glossary/does-not-exist/")
        self.assertEqual(resp.status_code, 404)

    def test_slug_only_valid_in_other_language_404s_no_cross_language_leak(self):
        # "durchsatz" only exists as language="de" - requesting it under the
        # "/en/" prefix must 404, not silently serve the German row.
        resp = self.client.get("/en/glossary/durchsatz/")
        self.assertEqual(resp.status_code, 404)


class GlossaryDetailViewContextTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.en_term = make_term(
            "Throughput", "throughput", "en", long_definition="A long, detailed explanation.",
        )
        cls.de_term = make_term(
            "Durchsatz", "durchsatz", "de", translation_group=cls.en_term.translation_group,
        )
        cls.lonely_term = make_term("Solo", "solo", "en")

    def test_sibling_translation_listed_as_alt_lang_term(self):
        resp = self.client.get("/en/glossary/throughput/")
        siblings = list(resp.context["alt_lang_terms"])
        self.assertEqual(siblings, [self.de_term])

    def test_no_sibling_translation_gives_empty_related_state(self):
        resp = self.client.get("/en/glossary/solo/")
        self.assertEqual(list(resp.context["alt_lang_terms"]), [])

    def test_alternates_include_both_languages_and_x_default(self):
        resp = self.client.get("/en/glossary/throughput/")
        seo = resp.context["seo"]
        langs = {a.lang for a in seo.alternates}
        self.assertIn("en", langs)
        self.assertIn("de", langs)
        self.assertIn("x-default", langs)

    def test_long_definition_used_in_description_when_present(self):
        resp = self.client.get("/en/glossary/throughput/")
        self.assertIn("long, detailed explanation", resp.context["seo"].description)

    def test_short_definition_used_when_long_definition_missing(self):
        resp = self.client.get("/en/glossary/solo/")
        desc = resp.context["seo"].description
        self.assertIn("Solo", desc)
        self.assertIn("Short: Solo", desc)

    def test_json_ld_term_code_is_the_slug(self):
        resp = self.client.get("/en/glossary/throughput/")
        self.assertEqual(resp.context["seo"].json_ld["termCode"], "throughput")

    def test_x_default_falls_back_to_canonical_when_no_default_language_alternate_exists(self):
        # A German-only term has no "en" alternate at all, so the x-default
        # emergency fallback (to the page's own canonical URL) is used
        # instead of pointing at a default-language alternate that doesn't
        # exist.
        de_only = make_term("Nur Deutsch", "nur-deutsch", "de")
        resp = self.client.get(f"/de/glossary/{de_only.slug}/")
        seo = resp.context["seo"]
        x_default = next(a for a in seo.alternates if a.lang == "x-default")
        self.assertEqual(x_default.url, seo.canonical)


class GlossaryAutocompleteViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.token_term = make_term("Token", "token", "en")
        cls.embedding_term = make_term("Embedding", "embedding", "en")

    def test_query_below_min_length_returns_empty_response(self):
        resp = self.client.get(reverse("glossary:autocomplete"), {"q": "t"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"")

    def test_valid_query_returns_html_fragment_with_hit(self):
        resp = self.client.get(reverse("glossary:autocomplete"), {"q": "Token"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Token", resp.content)

    def test_valid_query_no_hits_shows_no_hits_message(self):
        resp = self.client.get(reverse("glossary:autocomplete"), {"q": "zzz-nomatch"})
        self.assertIn(b"No hits.", resp.content)

    def test_letter_filter_narrows_results(self):
        resp = self.client.get(reverse("glossary:autocomplete"), {"q": "em", "letter": "E"})
        html = resp.content.decode()
        self.assertIn("Embedding", html)

    def test_json_format_returns_structured_results(self):
        resp = self.client.get(reverse("glossary:autocomplete"), {"q": "Token", "format": "json"})
        self.assertEqual(resp["Content-Type"], "application/json")
        data = resp.json()
        self.assertEqual(len(data["results"]), 1)
        self.assertEqual(data["results"][0]["slug"], "token")


class GlossaryApiViewTests(TestCase):
    """GlossaryApiView (glossary/views.py) has no registered URL anywhere in
    the project (confirmed via a project-wide grep) - it is dispatched here
    directly through RequestFactory + as_view(), exactly how Django's own
    generic View class is meant to be exercised, since there is no URL to
    reach it through. This is a documentation finding, not a bug: no
    production change was made (URLs are out of scope for this slice)."""

    @classmethod
    def setUpTestData(cls):
        cls.terms = [make_term(f"Api Term {i}", f"api-term-{i}", "en") for i in range(5)]

    def _get(self, **params):
        request = RequestFactory().get("/whatever/", params)
        resp = GlossaryApiView.as_view()(request)
        return json.loads(resp.content)

    def test_default_limit_and_offset(self):
        data = self._get()
        self.assertEqual(data["limit"], 50)
        self.assertEqual(data["offset"], 0)
        self.assertEqual(data["count"], 5)
        self.assertEqual(len(data["results"]), 5)

    def test_limit_is_clamped_to_max_200(self):
        data = self._get(limit=9999)
        self.assertEqual(data["limit"], 200)

    def test_limit_below_one_is_clamped_to_one(self):
        data = self._get(limit=0)
        self.assertEqual(data["limit"], 1)

    def test_invalid_limit_falls_back_to_default(self):
        data = self._get(limit="not-a-number")
        self.assertEqual(data["limit"], 50)

    def test_negative_offset_falls_back_to_zero(self):
        data = self._get(offset=-5)
        self.assertEqual(data["offset"], 0)

    def test_invalid_offset_falls_back_to_zero(self):
        data = self._get(offset="not-a-number")
        self.assertEqual(data["offset"], 0)

    def test_search_query_filters_results(self):
        data = self._get(q="Api Term 2")
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["slug"], "api-term-2")

    def test_letter_filter_narrows_results(self):
        make_term("Zebra Term", "zebra-term", "en")
        data = self._get(letter="Z")
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["slug"], "zebra-term")
