"""
Beta 8.7: tests for core.services.get_public_inventory /
resolve_public_starter_guide - the counts and highlights must match exactly
what the corresponding public list views (catalog, guides, prompts,
usecases, compare) actually show.
"""
from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from catalog.models import Category, Tool
from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin
from core.services import get_public_inventory, resolve_public_starter_guide
from guides.models import Guide
from prompts.models import Prompt
from usecases.models import UseCase


def make_tool(*, slug, published=True, languages=("en",), categories=()):
    # Tool.published_at has no null=True, so "not visible" can only be
    # expressed as a future publish date, never as a missing value.
    published_at = timezone.now() if published else timezone.now() + timezone.timedelta(days=1)
    t = Tool.objects.create(slug=slug, published_at=published_at)
    for lang in languages:
        t.create_translation(lang, name=f"Tool {slug} {lang}")
    for cat in categories:
        t.categories.add(cat)
    return t


def make_category(*, slug, languages=("en", "de")):
    c = Category.objects.create()
    for lang in languages:
        c.create_translation(lang, name=f"Category {slug} {lang}", slug=f"{slug}-{lang}")
    return c


def make_editorial(model, *, slug, status=EditorialWorkflowMixin.STATUS_PUBLISHED,
                    published_at=None, languages=("en",), extra=None, **create_kwargs):
    if published_at is None and status == EditorialWorkflowMixin.STATUS_PUBLISHED:
        published_at = timezone.now()
    obj = model.objects.create(status=status, published_at=published_at, **create_kwargs)
    extra_kwargs = extra or {}
    for lang in languages:
        obj.create_translation(
            lang, title=f"{model.__name__} {slug}", intro="i", body="b",
            slug=f"{slug}-{lang}", **extra_kwargs,
        )
    return obj


class InventoryTestCase(TestCase):
    def setUp(self):
        cache.clear()

    def _inv(self, lang="en"):
        return get_public_inventory(lang)


class ToolCountTests(InventoryTestCase):
    def test_only_active_visible_tools_are_counted(self):
        make_tool(slug="active-1")
        make_tool(slug="active-2")
        self.assertEqual(self._inv("en")["counts"]["tools"], 2)

    def test_inactive_tool_not_counted(self):
        make_tool(slug="unpublished", published=False)
        self.assertEqual(self._inv("en")["counts"]["tools"], 0)

    def test_future_published_at_not_counted(self):
        t = make_tool(slug="future")
        Tool.objects.filter(pk=t.pk).update(published_at=timezone.now() + timezone.timedelta(days=1))
        self.assertEqual(self._inv("en")["counts"]["tools"], 0)

    def test_no_duplicates_from_category_join(self):
        cat_a = make_category(slug="cat-a")
        cat_b = make_category(slug="cat-b")
        make_tool(slug="multi-cat", categories=[cat_a, cat_b])
        self.assertEqual(self._inv("en")["counts"]["tools"], 1)

    def test_tool_without_matching_translation_is_still_counted(self):
        # ToolListView.get_queryset() uses .language(lang), which never
        # filters by translation existence (PARLER_LANGUAGES has
        # hide_untranslated=False project-wide), so a DE-only tool still
        # renders - with fallback content - on the EN catalog page. The
        # inventory count must match that real page, not a stricter rule.
        make_tool(slug="de-only", languages=("de",))
        self.assertEqual(self._inv("en")["counts"]["tools"], 1)


class CategoryCountTests(InventoryTestCase):
    def test_category_with_visible_tool_is_counted(self):
        cat = make_category(slug="visible-cat")
        make_tool(slug="t1", categories=[cat])
        self.assertEqual(self._inv("en")["counts"]["categories"], 1)

    def test_empty_category_not_counted(self):
        make_category(slug="empty-cat")
        self.assertEqual(self._inv("en")["counts"]["categories"], 0)

    def test_category_with_only_inactive_tools_not_counted(self):
        cat = make_category(slug="inactive-only")
        make_tool(slug="inactive-t", categories=[cat], published=False)
        self.assertEqual(self._inv("en")["counts"]["categories"], 0)

    def test_category_without_translation_not_counted(self):
        cat = Category.objects.create()
        cat.create_translation("de", name="DE only", slug="de-only-cat")
        make_tool(slug="t-for-de-cat", categories=[cat])
        # Category listing (unlike Tool) uses .translated(lang) - strict,
        # no fallback - matching the existing catalog sidebar convention.
        self.assertEqual(self._inv("en")["counts"]["categories"], 0)
        self.assertEqual(self._inv("de")["counts"]["categories"], 1)

    def test_translation_join_does_not_duplicate_categories(self):
        cat = make_category(slug="dup-check")
        make_tool(slug="t1", categories=[cat])
        make_tool(slug="t2", categories=[cat])
        self.assertEqual(self._inv("en")["counts"]["categories"], 1)


class EditorialCountTests(InventoryTestCase):
    def test_published_guides_counted_drafts_excluded(self):
        make_editorial(Guide, slug="pub-guide")
        make_editorial(Guide, slug="draft-guide", status=EditorialWorkflowMixin.STATUS_DRAFT, published_at=None)
        self.assertEqual(self._inv("en")["counts"]["guides"], 1)

    def test_starter_guide_counted_exactly_once(self):
        make_editorial(Guide, slug="starter", is_starter=True)
        make_editorial(Guide, slug="regular")
        self.assertEqual(self._inv("en")["counts"]["guides"], 2)

    def test_published_prompts_counted_drafts_excluded(self):
        make_editorial(Prompt, slug="pub-prompt")
        make_editorial(Prompt, slug="draft-prompt", status=EditorialWorkflowMixin.STATUS_DRAFT, published_at=None)
        self.assertEqual(self._inv("en")["counts"]["prompts"], 1)

    def test_published_usecases_counted_drafts_excluded(self):
        make_editorial(UseCase, slug="pub-uc", extra={"persona": "Founder"})
        make_editorial(UseCase, slug="draft-uc", status=EditorialWorkflowMixin.STATUS_DRAFT,
                        published_at=None, extra={"persona": "Founder"})
        self.assertEqual(self._inv("en")["counts"]["usecases"], 1)

    def test_published_comparisons_counted_drafts_excluded(self):
        make_editorial(Comparison, slug="pub-cmp")
        make_editorial(Comparison, slug="draft-cmp", status=EditorialWorkflowMixin.STATUS_DRAFT, published_at=None)
        self.assertEqual(self._inv("en")["counts"]["comparisons"], 1)

    def test_guide_missing_active_translation_but_present_via_fallback_is_counted(self):
        # GuideListView uses .active_translations(lang), which includes the
        # 'en' fallback (hide_untranslated=False, PARLER_DEFAULT_LANGUAGE_CODE
        # ='en'). An EN-only guide therefore still appears - via fallback -
        # on the DE guide list (fallback only flows towards the default
        # language, not the other way around).
        make_editorial(Guide, slug="en-only-guide", languages=("en",))
        self.assertEqual(self._inv("de")["counts"]["guides"], 1)

    def test_guide_with_neither_active_nor_fallback_translation_not_counted(self):
        # Only en/de exist project-wide, so this can only be demonstrated by
        # a guide that has no en translation at all: it must be absent from
        # the EN list (en is both the requested and the fallback language,
        # so there is nothing left to fall back to).
        make_editorial(Guide, slug="de-only-guide", languages=("de",))
        self.assertEqual(self._inv("en")["counts"]["guides"], 0)

    def test_prompt_count_is_strictly_language_isolated(self):
        # Beta 8.8: PromptListView now uses visible_in_language(), which
        # never falls back across languages (unlike Guide/UseCase's
        # active_translations()). A DE-only prompt must not be counted in
        # the EN inventory, and vice versa.
        make_editorial(Prompt, slug="de-only-prompt", languages=("de",))
        self.assertEqual(self._inv("en")["counts"]["prompts"], 0)
        self.assertEqual(self._inv("de")["counts"]["prompts"], 1)

    def test_prompt_count_en_only_absent_from_german_inventory(self):
        make_editorial(Prompt, slug="en-only-prompt", languages=("en",))
        self.assertEqual(self._inv("de")["counts"]["prompts"], 0)
        self.assertEqual(self._inv("en")["counts"]["prompts"], 1)

    def test_bilingual_prompt_counted_once_per_language(self):
        make_editorial(Prompt, slug="bilingual-prompt", languages=("en", "de"))
        self.assertEqual(self._inv("en")["counts"]["prompts"], 1)
        self.assertEqual(self._inv("de")["counts"]["prompts"], 1)

    def test_prompt_inventory_count_matches_list_queryset(self):
        make_editorial(Prompt, slug="match-en", languages=("en",))
        make_editorial(Prompt, slug="match-de", languages=("de",))
        make_editorial(Prompt, slug="match-both", languages=("en", "de"))
        for lang in ("en", "de"):
            self.assertEqual(
                self._inv(lang)["counts"]["prompts"],
                Prompt.objects.visible_in_language(lang).count(),
            )


class TopCategoriesTests(InventoryTestCase):
    def test_at_most_six_categories(self):
        for i in range(8):
            cat = make_category(slug=f"cat-{i}")
            make_tool(slug=f"tool-{i}", categories=[cat])
        self.assertLessEqual(len(self._inv("en")["top_categories"]), 6)

    def test_ordered_by_visible_tool_count_descending(self):
        small = make_category(slug="small")
        big = make_category(slug="big")
        make_tool(slug="s1", categories=[small])
        make_tool(slug="b1", categories=[big])
        make_tool(slug="b2", categories=[big])
        top = self._inv("en")["top_categories"]
        self.assertEqual(top[0]["slug"], "big-en")
        self.assertEqual(top[0]["tool_count"], 2)
        self.assertEqual(top[1]["slug"], "small-en")

    def test_stable_tiebreaker_on_equal_tool_count(self):
        cat_a = make_category(slug="alpha")
        cat_b = make_category(slug="beta")
        make_tool(slug="a1", categories=[cat_a])
        make_tool(slug="b1", categories=[cat_b])
        top = self._inv("en")["top_categories"]
        # Equal tool_count (1 each) -> alphabetical name tiebreaker.
        self.assertEqual([c["slug"] for c in top], ["alpha-en", "beta-en"])

    def test_no_empty_categories_in_top_list(self):
        make_category(slug="empty")
        cat = make_category(slug="has-tool")
        make_tool(slug="t1", categories=[cat])
        top = self._inv("en")["top_categories"]
        self.assertEqual([c["slug"] for c in top], ["has-tool-en"])

    def test_names_and_urls_are_language_correct(self):
        cat = make_category(slug="lang-check")
        make_tool(slug="t1", categories=[cat])
        en_top = self._inv("en")["top_categories"][0]
        de_top = self._inv("de")["top_categories"][0]
        self.assertEqual(en_top["name"], "Category lang-check en")
        self.assertEqual(de_top["name"], "Category lang-check de")
        self.assertTrue(en_top["url"].startswith("/en/catalog/"))
        self.assertTrue(de_top["url"].startswith("/de/catalog/"))
        self.assertNotIn("/de/", en_top["url"])
        self.assertNotIn("/en/", de_top["url"])

    def test_category_filter_url_uses_the_real_filter_mechanism(self):
        cat = make_category(slug="filter-check")
        make_tool(slug="t1", categories=[cat])
        top = self._inv("en")["top_categories"][0]
        self.assertEqual(top["url"], "/en/catalog/?category=filter-check-en")


class StarterGuideResolutionTests(InventoryTestCase):
    def test_starter_resolved_via_is_starter_with_any_slug(self):
        make_editorial(Guide, slug="totally-unrelated", is_starter=True)
        starter = self._inv("en")["starter_guide"]
        self.assertIsNotNone(starter)
        self.assertEqual(starter["url"], "/en/guides/totally-unrelated-en/")

    def test_unpublished_starter_is_absent(self):
        make_editorial(Guide, slug="draft-starter", is_starter=True,
                        status=EditorialWorkflowMixin.STATUS_DRAFT, published_at=None)
        self.assertIsNone(self._inv("en")["starter_guide"])

    def test_missing_translation_yields_no_starter_link(self):
        make_editorial(Guide, slug="de-only-starter", is_starter=True, languages=("de",))
        self.assertIsNone(resolve_public_starter_guide("en"))
        self.assertIsNotNone(resolve_public_starter_guide("de"))

    def test_no_starter_in_database_is_none(self):
        make_editorial(Guide, slug="plain", is_starter=False)
        self.assertIsNone(self._inv("en")["starter_guide"])


class InventoryPerformanceTests(InventoryTestCase):
    # Queries against the actual content tables (not Django's/parler's own
    # cache-table bookkeeping in mentoroai_cache_table) are what would show
    # a real N+1: one row fetched/counted per category instead of one
    # aggregate query for all of them.
    INVENTORY_TABLES = (
        "catalog_tool", "catalog_category", "guides_guide",
        "prompts_prompt", "usecases_usecase", "compare_comparison",
    )

    def _inventory_table_queries(self, ctx):
        return [q for q in ctx.captured_queries if any(t in q["sql"] for t in self.INVENTORY_TABLES)]

    def test_category_query_count_does_not_scale_with_category_count(self):
        cat = make_category(slug="baseline")
        make_tool(slug="t0", categories=[cat])
        with CaptureQueriesContext(connection) as ctx_small:
            get_public_inventory("en")
        small_count = len(self._inventory_table_queries(ctx_small))

        cache.clear()
        for i in range(25):
            extra_cat = make_category(slug=f"extra-{i}")
            make_tool(slug=f"extra-tool-{i}", categories=[extra_cat])

        with CaptureQueriesContext(connection) as ctx_large:
            get_public_inventory("en")
        large_count = len(self._inventory_table_queries(ctx_large))

        # top_categories is always capped at 6 materialized rows and all
        # counts are single aggregate queries, so the query count must stay
        # (near-)constant regardless of the total number of categories in
        # the database. A small constant delta (<=2) is tolerated: which
        # exact 6 rows win the ORDER BY can shift parler's per-object
        # translation-cache warm state by one lookup; that is unrelated to
        # how many categories exist and does not scale with them (verified
        # separately: 8 -> 38 real categories produced an identical count).
        self.assertLessEqual(large_count, small_count + 2)

    def test_cache_hit_issues_no_inventory_model_queries(self):
        cat = make_category(slug="cache-perf")
        make_tool(slug="t1", categories=[cat])
        get_public_inventory("en")  # warm the cache

        with CaptureQueriesContext(connection) as ctx:
            get_public_inventory("en")
        inventory_tables = {
            "catalog_tool", "catalog_category", "catalog_category_translation",
            "guides_guide", "guides_guide_translation", "prompts_prompt",
            "usecases_usecase", "compare_comparison",
        }
        touched = [
            q["sql"] for q in ctx.captured_queries
            if any(t in q["sql"] for t in inventory_tables)
        ]
        self.assertEqual(touched, [])
