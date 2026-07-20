"""
Beta 8.14a: one temporal visibility rule for Tools, applied consistently.

Tool has no editorial status field; `published_at` alone decides public
visibility and doubles as the scheduled-publishing date:

    published_at <= now -> public
    published_at >  now -> not public

Reproduced BEFORE the fix (identical in EN and DE): a tool with a future
published_at was correctly hidden from the catalog list, the inventory
count and the per-category counts, but was still

  * reachable under its public detail URL with HTTP 200
    (ToolDetailView.get_object had no temporal filter at all),
  * rendered and linked in the homepage "featured tools" row
    (content/views/home.py had no temporal filter),
  * listed in both /en/sitemap.xml and /de/sitemap.xml
    (ToolSitemap.items returned Tool.objects.all()).

The sitemap entry only escaped being a broken URL because the detail view
shared the same gap and served it with 200 - the two defects masked each
other, which is why all three are closed together and asserted together
here.

A NULL published_at is impossible (NOT NULL column) and is asserted as
such, so there is no null-visibility case to define.

Tool's cross-language fallback is deliberately NOT touched by any of this -
see catalog/tests/test_language_fallback.py.
"""
import re

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from catalog.models import Category, Tool
from core.services import get_public_inventory

LOC_RE = re.compile(r"<loc>(.*?)</loc>")

PAST = -1
FUTURE = 7


def make_tool(slug, *, days, categories=(), featured=False, languages=("en", "de")):
    tool = Tool.objects.create(
        slug=slug,
        published_at=timezone.now() + timezone.timedelta(days=days),
        is_featured=featured,
    )
    for lang in languages:
        tool.create_translation(lang, name=f"Tool {slug} {lang}", short_description="s")
    for category in categories:
        tool.categories.add(category)
    return tool


def make_category(slug):
    category = Category.objects.create()
    for lang in ("en", "de"):
        category.create_translation(lang, name=f"Cat {slug} {lang}", slug=f"{slug}-{lang}")
    return category


# ---------------------------------------------------------------------------
# A. The central query
# ---------------------------------------------------------------------------

class ToolQuerySetPublicTests(TestCase):
    def test_past_tool_is_public(self):
        tool = make_tool("pq-past", days=PAST)
        self.assertIn(tool, Tool.objects.public())

    def test_tool_published_just_now_is_public(self):
        tool = Tool.objects.create(slug="pq-now", published_at=timezone.now())
        self.assertIn(tool, Tool.objects.public())

    def test_future_tool_is_not_public(self):
        tool = make_tool("pq-future", days=FUTURE)
        self.assertNotIn(tool, Tool.objects.public())

    def test_null_published_at_cannot_exist(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Tool.objects.create(slug="pq-null", published_at=None)

    def test_public_accepts_an_explicit_point_in_time(self):
        tool = make_tool("pq-at", days=FUTURE)
        later = timezone.now() + timezone.timedelta(days=FUTURE + 1)
        self.assertNotIn(tool, Tool.objects.public())
        self.assertIn(tool, Tool.objects.public(at=later))

    def test_public_is_chainable_and_imposes_no_ordering(self):
        make_tool("pq-chain", days=PAST)
        qs = Tool.objects.public().language("en").filter(slug="pq-chain")
        self.assertEqual(qs.count(), 1)
        self.assertEqual(Tool.objects.public().query.order_by, ())


# ---------------------------------------------------------------------------
# B-F. Every public surface agrees with that rule
# ---------------------------------------------------------------------------

class PublicToolSurfaceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cat_public = make_category("vis-cat-public")
        cls.cat_future_only = make_category("vis-cat-future")
        cls.cat_mixed = make_category("vis-cat-mixed")

        cls.past = make_tool(
            "vis-past", days=PAST, categories=[cls.cat_public, cls.cat_mixed], featured=True
        )
        cls.now = make_tool("vis-now", days=0, categories=[cls.cat_public])
        cls.future = make_tool(
            "vis-future", days=FUTURE,
            categories=[cls.cat_future_only, cls.cat_mixed], featured=True,
        )

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    # -- catalog list --

    def test_catalog_lists_public_tools_and_hides_future_ones(self):
        for lang in ("en", "de"):
            html = self.client.get(f"/{lang}/catalog/").content.decode()
            with self.subTest(lang=lang):
                self.assertIn(f"/{lang}/catalog/vis-past/", html)
                self.assertIn(f"/{lang}/catalog/vis-now/", html)
                self.assertNotIn("vis-future", html)

    def test_catalog_paginator_count_matches_the_public_queryset(self):
        for lang in ("en", "de"):
            resp = self.client.get(f"/{lang}/catalog/")
            with self.subTest(lang=lang):
                self.assertEqual(resp.context["paginator"].count, Tool.objects.public().count())

    def test_category_filter_never_surfaces_a_future_tool(self):
        for lang in ("en", "de"):
            slug = f"vis-cat-future-{lang}"
            html = self.client.get(f"/{lang}/catalog/?category={slug}").content.decode()
            with self.subTest(lang=lang):
                self.assertNotIn("vis-future", html)

    # -- detail --

    def test_public_tool_detail_returns_200(self):
        for lang in ("en", "de"):
            for slug in ("vis-past", "vis-now"):
                with self.subTest(lang=lang, slug=slug):
                    self.assertEqual(self.client.get(f"/{lang}/catalog/{slug}/").status_code, 200)

    def test_future_tool_detail_returns_404(self):
        for lang in ("en", "de"):
            with self.subTest(lang=lang):
                self.assertEqual(
                    self.client.get(f"/{lang}/catalog/vis-future/").status_code, 404
                )

    def test_public_tool_detail_canonical_uses_its_own_url(self):
        for lang in ("en", "de"):
            html = self.client.get(f"/{lang}/catalog/vis-past/").content.decode()
            with self.subTest(lang=lang):
                self.assertIn(f"/{lang}/catalog/vis-past/", html)

    def test_related_tools_never_include_a_future_tool(self):
        # vis-past and vis-future share cat_mixed
        for lang in ("en", "de"):
            html = self.client.get(f"/{lang}/catalog/vis-past/").content.decode()
            with self.subTest(lang=lang):
                self.assertNotIn("vis-future", html)

    # -- inventory --

    def test_inventory_tool_count_matches_catalog_and_excludes_future(self):
        for lang in ("en", "de"):
            counts = get_public_inventory(lang)["counts"]
            rendered = self.client.get(f"/{lang}/catalog/").context["paginator"].count
            with self.subTest(lang=lang):
                self.assertEqual(counts["tools"], Tool.objects.public().count())
                self.assertEqual(counts["tools"], rendered)

    # -- category counts --

    def test_category_counts_ignore_future_tools(self):
        for lang in ("en", "de"):
            by_name = {
                c["name"]: c["tool_count"] for c in get_public_inventory(lang)["top_categories"]
            }
            with self.subTest(lang=lang):
                self.assertEqual(by_name.get(f"Cat vis-cat-public {lang}"), 2)
                # mixed category counts only its one public tool
                self.assertEqual(by_name.get(f"Cat vis-cat-mixed {lang}"), 1)
                # a category holding nothing but future tools is not well-stocked
                self.assertNotIn(f"Cat vis-cat-future {lang}", by_name)

    def test_category_with_only_future_tools_is_not_counted_as_stocked(self):
        for lang in ("en", "de"):
            counts = get_public_inventory(lang)["counts"]
            with self.subTest(lang=lang):
                # cat_public + cat_mixed, never cat_future_only
                self.assertEqual(counts["categories"], 2)

    # -- sitemap --

    def test_sitemap_contains_public_tools_only(self):
        for lang in ("en", "de"):
            locs = LOC_RE.findall(self.client.get(f"/{lang}/sitemap.xml").content.decode())
            with self.subTest(lang=lang):
                self.assertTrue(any("vis-past" in u for u in locs))
                self.assertTrue(any("vis-now" in u for u in locs))
                self.assertFalse(any("vis-future" in u for u in locs))

    def test_every_tool_sitemap_url_returns_200_and_is_unique(self):
        for lang in ("en", "de"):
            locs = [
                u for u in LOC_RE.findall(
                    self.client.get(f"/{lang}/sitemap.xml").content.decode()
                )
                if "/catalog/" in u
            ]
            with self.subTest(lang=lang):
                self.assertEqual(len(locs), len(set(locs)), "duplicate tool sitemap entries")
                for loc in locs:
                    path = re.sub(r"^https?://[^/]+", "", loc)
                    self.assertEqual(self.client.get(path).status_code, 200, path)

    # -- homepage featured row --

    def test_homepage_featured_tools_exclude_future_tools(self):
        for lang in ("en", "de"):
            html = self.client.get(f"/{lang}/").content.decode()
            with self.subTest(lang=lang):
                self.assertIn(f"/{lang}/catalog/vis-past/", html)
                self.assertNotIn("vis-future", html)
