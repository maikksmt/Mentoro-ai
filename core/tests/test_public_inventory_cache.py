"""
Beta 8.7/8.8: cache behaviour of core.services.get_public_inventory,
including the Beta 8.8 v1 -> v2 cache key version bump.
"""
import pickle

from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from catalog.models import Category, Tool
from core.services import (
    PUBLIC_INVENTORY_CACHE_TIMEOUT,
    PUBLIC_INVENTORY_CACHE_VERSION,
    get_public_inventory,
)

INVENTORY_TABLES = (
    "catalog_tool", "catalog_category", "guides_guide",
    "prompts_prompt", "usecases_usecase", "compare_comparison",
)


def _inventory_table_queries(ctx):
    return [q for q in ctx.captured_queries if any(t in q["sql"] for t in INVENTORY_TABLES)]


class PublicInventoryCacheTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_cold_cache_computes_and_stores_data(self):
        cache_key = f"mentoroai:public-inventory:{PUBLIC_INVENTORY_CACHE_VERSION}:en"
        self.assertIsNone(cache.get(cache_key))
        data = get_public_inventory("en")
        self.assertIsNotNone(data)
        self.assertEqual(cache.get(cache_key), data)

    def test_second_call_same_language_hits_cache_without_inventory_queries(self):
        get_public_inventory("en")  # cold
        with CaptureQueriesContext(connection) as ctx:
            second = get_public_inventory("en")
        self.assertEqual(_inventory_table_queries(ctx), [])
        self.assertIsNotNone(second)

    def test_english_and_german_use_separate_cache_keys(self):
        get_public_inventory("en")
        get_public_inventory("de")
        en_key = f"mentoroai:public-inventory:{PUBLIC_INVENTORY_CACHE_VERSION}:en"
        de_key = f"mentoroai:public-inventory:{PUBLIC_INVENTORY_CACHE_VERSION}:de"
        self.assertIsNotNone(cache.get(en_key))
        self.assertIsNotNone(cache.get(de_key))

    def test_english_and_german_do_not_share_content(self):
        cat = Category.objects.create()
        cat.create_translation("en", name="English Name", slug="lang-cache-en")
        cat.create_translation("de", name="Deutscher Name", slug="lang-cache-de")
        t = Tool.objects.create(slug="lang-cache-tool", published_at=timezone.now())
        t.create_translation("en", name="Tool EN")
        t.categories.add(cat)

        en_data = get_public_inventory("en")
        de_data = get_public_inventory("de")
        en_names = [c["name"] for c in en_data["top_categories"]]
        de_names = [c["name"] for c in de_data["top_categories"]]
        self.assertIn("English Name", en_names)
        self.assertNotIn("Deutscher Name", en_names)
        self.assertIn("Deutscher Name", de_names)
        self.assertNotIn("English Name", de_names)

    def test_returned_value_contains_no_querysets_or_model_instances(self):
        from django.db.models import Model, QuerySet

        data = get_public_inventory("en")

        def assert_primitive(value):
            self.assertNotIsInstance(value, (Model, QuerySet))
            if isinstance(value, dict):
                for v in value.values():
                    assert_primitive(v)
            elif isinstance(value, list):
                for v in value:
                    assert_primitive(v)

        assert_primitive(data)

    def test_returned_value_is_picklable_like_any_cache_backend_would_require(self):
        # DatabaseCache (this project's backend) always pickles values; a
        # QuerySet or bound model instance would still "work" here but the
        # explicit type check above is the real guard - this just confirms
        # the dict round-trips cleanly through pickle as an extra safety net.
        data = get_public_inventory("en")
        restored = pickle.loads(pickle.dumps(data))
        self.assertEqual(data, restored)

    def test_cache_timeout_matches_configured_value(self):
        cache_key = f"mentoroai:public-inventory:{PUBLIC_INVENTORY_CACHE_VERSION}:en"
        get_public_inventory("en")
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT expires FROM mentoroai_cache_table WHERE cache_key = %s',
                [cache.make_key(cache_key)],
            )
            row = cursor.fetchone()
        self.assertIsNotNone(row)
        expires = row[0]
        now = timezone.now()
        delta_seconds = (expires - now).total_seconds()
        # Allow a little slack for test execution time.
        self.assertGreater(delta_seconds, PUBLIC_INVENTORY_CACHE_TIMEOUT - 10)
        self.assertLessEqual(delta_seconds, PUBLIC_INVENTORY_CACHE_TIMEOUT + 1)

    def test_cache_miss_after_manual_expiry_recomputes(self):
        cache_key = f"mentoroai:public-inventory:{PUBLIC_INVENTORY_CACHE_VERSION}:en"
        get_public_inventory("en")
        cache.delete(cache_key)
        with CaptureQueriesContext(connection) as ctx:
            get_public_inventory("en")
        self.assertGreater(len(_inventory_table_queries(ctx)), 0)

    def test_service_works_with_an_empty_cache_on_every_call(self):
        # Simulates a cache backend that never retains anything.
        for _ in range(3):
            cache.clear()
            data = get_public_inventory("en")
            self.assertIn("counts", data)


class PublicInventoryCacheVersionBumpTests(TestCase):
    """
    Beta 8.8: the prompt count's semantics changed (language-independent ->
    strictly language-isolated), so any pre-existing v1 cache entry holds a
    now-wrong value. Bumping the cache key version (v1 -> v2) guarantees a
    stale v1 entry is never served again, without a blanket cache.clear().
    """

    def setUp(self):
        cache.clear()

    def test_cache_key_uses_v2(self):
        self.assertEqual(PUBLIC_INVENTORY_CACHE_VERSION, "v2")

    def test_new_value_is_stored_under_the_v2_key(self):
        get_public_inventory("en")
        self.assertIsNotNone(cache.get("mentoroai:public-inventory:v2:en"))

    def test_stale_v1_entry_is_never_read(self):
        # Simulate a stale pre-deployment cache entry holding the old,
        # semantically wrong (language-independent) prompt count.
        stale_v1_value = {
            "counts": {"tools": 0, "categories": 0, "guides": 0, "prompts": 999,
                       "usecases": 0, "comparisons": 0},
            "top_categories": [],
            "starter_guide": None,
        }
        cache.set("mentoroai:public-inventory:v1:en", stale_v1_value, timeout=300)

        data = get_public_inventory("en")

        self.assertNotEqual(data["counts"]["prompts"], 999)
        # The stale v1 entry is left untouched (no blanket cache.clear()).
        self.assertEqual(cache.get("mentoroai:public-inventory:v1:en"), stale_v1_value)
