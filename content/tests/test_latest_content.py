"""
Beta 8.9/8.9a: get_latest_items() ("Aktuelle Inhalte" on the homepage) must
be language-safe for all four editorial content types. Prompts/Comparisons
were hardened in Beta 8.9; Guides/UseCases were confirmed via empirical
testing in Beta 8.9a to leak EN-only content onto the DE homepage (and vice
versa) under their previous .published manager (ambient-language
active_translations() fallback) - the generated link did not 404, it
silently rendered the wrong language under the active prefix. All four now
use visible_in_language() (strict, no cross-language fallback) inside
get_latest_items() specifically. GuideListView/GuideDetailView keep their
existing, unchanged behavior (out of Beta 8.9a's scope - only the homepage
teaser leak was confirmed and fixed for Guide). UseCaseListView/
UseCaseDetailView were separately hardened too, for the related persona-
crash fix (see usecases/tests/test_detail_language_safety.py). Tools are
not part of get_latest_items() at all (rendered separately as "Featured
Tools").
"""
from django.test import TestCase
from django.utils import timezone
from django.utils.translation import get_language
from parler.utils.context import switch_language

from catalog.models import Tool
from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin
from core.services import get_latest_items
from guides.models import Guide
from prompts.models import Prompt
from usecases.models import UseCase


def make_prompt(*, slug, languages=("en",), status=EditorialWorkflowMixin.STATUS_PUBLISHED, published_at=None):
    if published_at is None and status == EditorialWorkflowMixin.STATUS_PUBLISHED:
        published_at = timezone.now()
    p = Prompt.objects.create(status=status, published_at=published_at)
    for lang in languages:
        p.create_translation(lang, title=f"Prompt {slug}", intro="i", body="b", slug=f"{slug}-{lang}")
    return p


def make_comparison(*, slug, languages=("en",), status=EditorialWorkflowMixin.STATUS_PUBLISHED, published_at=None):
    if published_at is None and status == EditorialWorkflowMixin.STATUS_PUBLISHED:
        published_at = timezone.now()
    c = Comparison.objects.create(status=status, published_at=published_at)
    for lang in languages:
        c.create_translation(lang, title=f"Comparison {slug}", intro="i", body="b", slug=f"{slug}-{lang}")
    return c


def make_guide(*, slug, languages=("en",), status=EditorialWorkflowMixin.STATUS_PUBLISHED, published_at=None):
    if published_at is None and status == EditorialWorkflowMixin.STATUS_PUBLISHED:
        published_at = timezone.now()
    g = Guide.objects.create(status=status, published_at=published_at)
    for lang in languages:
        g.create_translation(lang, title=f"Guide {slug}", intro="i", body="b", slug=f"{slug}-{lang}")
    return g


def make_usecase(*, slug, languages=("en",), status=EditorialWorkflowMixin.STATUS_PUBLISHED, published_at=None):
    if published_at is None and status == EditorialWorkflowMixin.STATUS_PUBLISHED:
        published_at = timezone.now()
    u = UseCase.objects.create(status=status, published_at=published_at)
    for lang in languages:
        u.create_translation(lang, title=f"Usecase {slug}", intro="i", body="b", outro="o",
                              slug=f"{slug}-{lang}", persona="")
    return u


class LatestContentPromptLanguageTests(TestCase):
    def test_en_only_prompt_absent_from_german_latest_items(self):
        make_prompt(slug="en-only-latest", languages=("en",))
        items = get_latest_items(limit=20, language_code="de")
        self.assertFalse(any(i["badge"] == "Prompt" and "en-only-latest" in i["url"] for i in items))

    def test_de_only_prompt_absent_from_english_latest_items(self):
        make_prompt(slug="de-only-latest", languages=("de",))
        items = get_latest_items(limit=20, language_code="en")
        self.assertFalse(any(i["badge"] == "Prompt" and "de-only-latest" in i["url"] for i in items))

    def test_en_prompt_present_in_english_latest_items(self):
        make_prompt(slug="present-en", languages=("en",))
        items = get_latest_items(limit=20, language_code="en")
        self.assertTrue(any(i["badge"] == "Prompt" and "present-en" in i["url"] for i in items))

    def test_draft_prompt_never_appears(self):
        make_prompt(slug="draft-latest", status=EditorialWorkflowMixin.STATUS_DRAFT,
                    published_at=None, languages=("en", "de"))
        items_en = get_latest_items(limit=20, language_code="en")
        items_de = get_latest_items(limit=20, language_code="de")
        self.assertFalse(any("draft-latest" in i["url"] for i in items_en))
        self.assertFalse(any("draft-latest" in i["url"] for i in items_de))

    def test_all_prompt_links_in_latest_items_are_reachable(self):
        for i in range(5):
            make_prompt(slug=f"reach-{i}", languages=("en",))
        items = get_latest_items(limit=20, language_code="en")
        prompt_items = [i for i in items if i["badge"] == "Prompt"]
        for item in prompt_items:
            resp = self.client.get(item["url"])
            self.assertEqual(resp.status_code, 200)


class LatestContentComparisonLanguageTests(TestCase):
    def test_en_only_comparison_absent_from_german_latest_items(self):
        make_comparison(slug="en-only-cmp-latest", languages=("en",))
        items = get_latest_items(limit=20, language_code="de")
        self.assertFalse(any(i["badge"] == "Comparison" and "en-only-cmp-latest" in i["url"] for i in items))

    def test_de_only_comparison_absent_from_english_latest_items(self):
        make_comparison(slug="de-only-cmp-latest", languages=("de",))
        items = get_latest_items(limit=20, language_code="en")
        self.assertFalse(any(i["badge"] == "Comparison" and "de-only-cmp-latest" in i["url"] for i in items))

    def test_all_comparison_links_in_latest_items_are_reachable(self):
        for i in range(5):
            make_comparison(slug=f"cmp-reach-{i}", languages=("en",))
        items = get_latest_items(limit=20, language_code="en")
        cmp_items = [i for i in items if i["badge"] == "Comparison"]
        for item in cmp_items:
            resp = self.client.get(item["url"])
            self.assertEqual(resp.status_code, 200)


class LatestContentGuideLanguageTests(TestCase):
    def test_guide_with_active_translation_appears_en(self):
        make_guide(slug="active-guide-en", languages=("en",))
        items = get_latest_items(limit=20, language_code="en")
        self.assertTrue(any(i["badge"] == "Guide" and "active-guide-en" in i["url"] for i in items))

    def test_guide_without_active_translation_excluded(self):
        make_guide(slug="inactive-guide", languages=("de",))
        items = get_latest_items(limit=20, language_code="en")
        self.assertFalse(any("inactive-guide" in i["url"] for i in items))

    def test_en_only_guide_absent_from_german_latest_items(self):
        make_guide(slug="en-only-guide-latest", languages=("en",))
        items = get_latest_items(limit=20, language_code="de")
        self.assertFalse(any(i["badge"] == "Guide" and "en-only-guide-latest" in i["url"] for i in items))

    def test_de_only_guide_absent_from_english_latest_items(self):
        make_guide(slug="de-only-guide-latest", languages=("de",))
        items = get_latest_items(limit=20, language_code="en")
        self.assertFalse(any(i["badge"] == "Guide" and "de-only-guide-latest" in i["url"] for i in items))

    def test_bilingual_guide_appears_in_both_languages(self):
        make_guide(slug="bilingual-guide-latest", languages=("en", "de"))
        items_en = get_latest_items(limit=20, language_code="en")
        items_de = get_latest_items(limit=20, language_code="de")
        self.assertTrue(any("bilingual-guide-latest" in i["url"] for i in items_en))
        self.assertTrue(any("bilingual-guide-latest" in i["url"] for i in items_de))

    def test_draft_guide_never_appears(self):
        make_guide(slug="draft-guide-latest", status=EditorialWorkflowMixin.STATUS_DRAFT,
                   published_at=None, languages=("en", "de"))
        items_en = get_latest_items(limit=20, language_code="en")
        items_de = get_latest_items(limit=20, language_code="de")
        self.assertFalse(any("draft-guide-latest" in i["url"] for i in items_en))
        self.assertFalse(any("draft-guide-latest" in i["url"] for i in items_de))

    def test_all_guide_links_in_latest_items_are_reachable(self):
        for i in range(5):
            make_guide(slug=f"guide-reach-{i}", languages=("en",))
        items = get_latest_items(limit=20, language_code="en")
        guide_items = [i for i in items if i["badge"] == "Guide"]
        for item in guide_items:
            resp = self.client.get(item["url"])
            self.assertEqual(resp.status_code, 200)

    def test_no_wrong_language_prefix_in_guide_links(self):
        make_guide(slug="guide-prefix-check", languages=("en",))
        items = get_latest_items(limit=20, language_code="en")
        guide_items = [i for i in items if i["badge"] == "Guide" and "guide-prefix-check" in i["url"]]
        for item in guide_items:
            self.assertTrue(item["url"].startswith("/en/"))


class LatestContentUseCaseLanguageTests(TestCase):
    def test_usecase_with_active_translation_appears_en(self):
        make_usecase(slug="active-uc-en", languages=("en",))
        items = get_latest_items(limit=20, language_code="en")
        self.assertTrue(any(i["badge"] == "Usecase" and "active-uc-en" in i["url"] for i in items))

    def test_usecase_without_active_translation_excluded(self):
        make_usecase(slug="inactive-uc", languages=("de",))
        items = get_latest_items(limit=20, language_code="en")
        self.assertFalse(any("inactive-uc" in i["url"] for i in items))

    def test_en_only_usecase_absent_from_german_latest_items(self):
        make_usecase(slug="en-only-uc-latest", languages=("en",))
        items = get_latest_items(limit=20, language_code="de")
        self.assertFalse(any(i["badge"] == "Usecase" and "en-only-uc-latest" in i["url"] for i in items))

    def test_de_only_usecase_absent_from_english_latest_items(self):
        make_usecase(slug="de-only-uc-latest", languages=("de",))
        items = get_latest_items(limit=20, language_code="en")
        self.assertFalse(any(i["badge"] == "Usecase" and "de-only-uc-latest" in i["url"] for i in items))

    def test_bilingual_usecase_appears_in_both_languages(self):
        make_usecase(slug="bilingual-uc-latest", languages=("en", "de"))
        items_en = get_latest_items(limit=20, language_code="en")
        items_de = get_latest_items(limit=20, language_code="de")
        self.assertTrue(any("bilingual-uc-latest" in i["url"] for i in items_en))
        self.assertTrue(any("bilingual-uc-latest" in i["url"] for i in items_de))

    def test_draft_usecase_never_appears(self):
        make_usecase(slug="draft-uc-latest", status=EditorialWorkflowMixin.STATUS_DRAFT,
                     published_at=None, languages=("en", "de"))
        items_en = get_latest_items(limit=20, language_code="en")
        items_de = get_latest_items(limit=20, language_code="de")
        self.assertFalse(any("draft-uc-latest" in i["url"] for i in items_en))
        self.assertFalse(any("draft-uc-latest" in i["url"] for i in items_de))

    def test_all_usecase_links_in_latest_items_are_reachable(self):
        for i in range(5):
            make_usecase(slug=f"uc-reach-{i}", languages=("en",))
        items = get_latest_items(limit=20, language_code="en")
        uc_items = [i for i in items if i["badge"] == "Usecase"]
        for item in uc_items:
            resp = self.client.get(item["url"])
            self.assertEqual(resp.status_code, 200)

    def test_no_wrong_slugs_in_usecase_links(self):
        make_usecase(slug="uc-slug-check", languages=("en",))
        items = get_latest_items(limit=20, language_code="en")
        uc_items = [i for i in items if i["badge"] == "Usecase" and "uc-slug-check" in i["url"]]
        for item in uc_items:
            self.assertTrue(item["url"].endswith("uc-slug-check-en/"))


class LatestContentNoDuplicatesTests(TestCase):
    def test_no_duplicate_items_by_type_and_slug(self):
        for i in range(6):
            make_prompt(slug=f"noDup-{i}", languages=("en",))
            make_comparison(slug=f"noDupC-{i}", languages=("en",))
            make_guide(slug=f"noDupG-{i}", languages=("en",))
            make_usecase(slug=f"noDupU-{i}", languages=("en",))
        items = get_latest_items(limit=20, language_code="en")
        keys = [(i["badge"], i["url"]) for i in items]
        self.assertEqual(len(keys), len(set(keys)))


class LatestContentActiveLanguageUnchangedAfterCallTests(TestCase):
    """Beta 8.9a Section G: active Django language must be unchanged after
    get_latest_items() returns, regardless of the explicit language_code
    passed in (the function wraps its body in override(lang) internally)."""

    def test_ambient_language_restored_after_call_with_different_code(self):
        from django.utils import translation
        with translation.override("en"):
            before = get_language()
            get_latest_items(limit=10, language_code="de")
            after = get_language()
            self.assertEqual(before, after)
            self.assertEqual(after, "en")


class ToolsFallbackPreservedOnHomepageTests(TestCase):
    """Beta 8.9: Tools are a deliberate, unchanged exception - they are not
    part of get_latest_items() at all (rendered separately as "Featured
    Tools" via Tool.objects.filter(is_featured=True), no language
    filtering). This slice must not tighten that."""

    def _make_featured_tool(self, *, slug, name):
        tool = Tool.objects.create(slug=slug, website="https://example.com/" + slug, is_featured=True)
        with switch_language(tool, "en"):
            tool.name = name
            tool.short_description = "s"
            tool.long_description = "l"
            tool.save()
        return tool

    def test_english_only_tool_still_appears_on_german_homepage(self):
        self._make_featured_tool(slug="fallback-tool-en-only", name="Fallback Tool")
        resp = self.client.get("/de/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Fallback Tool", resp.content.decode())

    def test_featured_tools_not_filtered_out_by_editorial_language_change(self):
        self._make_featured_tool(slug="featured-check", name="Featured Check Tool")
        resp = self.client.get("/en/")
        featured = list(resp.context["featured_tools"])
        self.assertTrue(any(t.slug == "featured-check" for t in featured))


class LatestContentEnglishGermanIsolationTests(TestCase):
    def test_homepage_shows_different_latest_content_per_language(self):
        make_prompt(slug="only-en-home", languages=("en",))
        make_comparison(slug="only-de-home", languages=("de",))

        resp_en = self.client.get("/en/")
        resp_de = self.client.get("/de/")

        self.assertEqual(resp_en.status_code, 200)
        self.assertEqual(resp_de.status_code, 200)

        html_en = resp_en.content.decode()
        html_de = resp_de.content.decode()

        self.assertIn("only-en-home", html_en)
        self.assertNotIn("only-en-home", html_de)
        self.assertIn("only-de-home", html_de)
        self.assertNotIn("only-de-home", html_en)


class LatestContentMixAndLimitPreservedTests(TestCase):
    """Beta 8.9a Section G: the existing default mix (4,3,2,1) and the
    overall limit must be unaffected by the Guide/UseCase language fix."""

    def test_default_limit_of_six_is_still_respected(self):
        for i in range(10):
            make_guide(slug=f"mix-guide-{i}", languages=("en",))
            make_prompt(slug=f"mix-prompt-{i}", languages=("en",))
            make_usecase(slug=f"mix-uc-{i}", languages=("en",))
            make_comparison(slug=f"mix-cmp-{i}", languages=("en",))
        items = get_latest_items(language_code="en")
        self.assertLessEqual(len(items), 6)

    def test_all_four_types_can_still_appear_together(self):
        make_guide(slug="mix-check-guide", languages=("en",))
        make_prompt(slug="mix-check-prompt", languages=("en",))
        make_usecase(slug="mix-check-uc", languages=("en",))
        make_comparison(slug="mix-check-cmp", languages=("en",))
        items = get_latest_items(limit=10, language_code="en")
        badges = {i["badge"] for i in items}
        self.assertEqual(badges, {"Guide", "Prompt", "Usecase", "Comparison"})
