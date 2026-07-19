"""
Beta 8.7: global footer structure (Explore / Browse categories / Start here
/ Legal), semantics, and preserved pre-existing functionality.
"""
import re

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from catalog.models import Category, Tool
from guides.models import Guide


def make_guide(*, slug, is_starter=False):
    g = Guide.objects.create(status="published", published_at=timezone.now(), is_starter=is_starter)
    g.create_translation("en", title="Guide", intro="i", body="b", slug=f"{slug}-en")
    g.create_translation("de", title="Guide DE", intro="i", body="b", slug=f"{slug}-de")
    return g


def make_tool(*, slug, categories=()):
    t = Tool.objects.create(slug=slug, published_at=timezone.now())
    t.create_translation("en", name=f"Tool {slug}")
    for cat in categories:
        t.categories.add(cat)
    return t


def make_category(*, slug):
    c = Category.objects.create()
    c.create_translation("en", name=f"Category {slug}", slug=f"{slug}-en")
    c.create_translation("de", name=f"Kategorie {slug}", slug=f"{slug}-de")
    return c


class FooterStructureTests(TestCase):
    def setUp(self):
        cache.clear()

    def _footer_html(self, path="/en/"):
        resp = self.client.get(path)
        html = resp.content.decode()
        return html[html.find("<!-- Footer -->"):]

    def test_footer_has_the_four_groups(self):
        html = self._footer_html()
        self.assertIn('id="footer-explore-heading"', html)
        self.assertIn(">Explore<", html)
        self.assertIn('id="footer-starter-heading"', html)
        self.assertIn(">Start here<", html)
        self.assertIn('id="footer-legal-heading"', html)
        self.assertIn(">Legal<", html)

    def test_browse_categories_group_present_when_categories_exist(self):
        cat = make_category(slug="present")
        make_tool(slug="t1", categories=[cat])
        cache.clear()
        html = self._footer_html()
        self.assertIn('id="footer-categories-heading"', html)
        self.assertIn(">Browse categories<", html)

    def test_browse_categories_group_absent_when_no_categories(self):
        Category.objects.all().delete()
        cache.clear()
        html = self._footer_html()
        self.assertNotIn('id="footer-categories-heading"', html)

    def test_link_groups_are_lists_inside_nav_elements(self):
        html = self._footer_html()
        self.assertIn('<nav aria-labelledby="footer-explore-heading">', html)
        explore_start = html.find('<nav aria-labelledby="footer-explore-heading">')
        explore_end = html.find("</nav>", explore_start)
        explore_html = html[explore_start:explore_end]
        self.assertIn("<ul", explore_html)
        self.assertIn("<li>", explore_html)

    def test_no_menu_roles_or_clickable_divs(self):
        html = self._footer_html()
        self.assertNotIn('role="menu"', html)
        self.assertNotIn('role="menuitem"', html)

    def test_no_duplicate_ids(self):
        html = self._footer_html()
        ids = re.findall(r'id="([^"]+)"', html)
        self.assertEqual(len(ids), len(set(ids)), f"Duplicate ids found: {ids}")

    def test_mobile_and_desktop_share_the_same_markup(self):
        # No separate mobile/desktop footer templates - responsive behavior
        # is CSS-only (grid-cols-2 on mobile already, lg:grid-cols-4 on
        # desktop), so a single server-rendered response already contains
        # everything for every viewport.
        html = self._footer_html()
        self.assertIn("grid-cols-2", html)
        self.assertIn("lg:grid-cols-4", html)


class FooterExploreGroupTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_all_six_explore_links_present_with_correct_language_urls(self):
        resp = self.client.get("/de/")
        html = resp.content.decode()
        footer = html[html.find("<!-- Footer -->"):]
        self.assertIn(reverse("catalog:list"), footer)
        self.assertIn(reverse("guides:list"), footer)
        self.assertIn(reverse("prompts:list"), footer)
        self.assertIn(reverse("usecases:list"), footer)
        self.assertIn(reverse("compare:index"), footer)
        self.assertIn(reverse("glossary:list"), footer)
        # All URLs above already carry the /de/ prefix from reverse() under
        # the active language; double-check none accidentally use /en/.
        explore_start = footer.find('id="footer-explore-heading"')
        explore_end = footer.find("</nav>", explore_start)
        self.assertNotIn("/en/", footer[explore_start:explore_end])


class FooterCategoriesTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_only_real_top_categories_no_empty_links(self):
        cat = make_category(slug="only-one")
        make_tool(slug="t1", categories=[cat])
        cache.clear()
        html_bytes = self.client.get("/en/").content.decode()
        footer = html_bytes[html_bytes.find("<!-- Footer -->"):]
        cat_start = footer.find('id="footer-categories-heading"')
        cat_end = footer.find("</nav>", cat_start)
        cat_html = footer[cat_start:cat_end]
        self.assertNotIn('href=""', cat_html)
        self.assertIn("Category only-one", cat_html)

    def test_at_most_six_category_links(self):
        for i in range(9):
            cat = make_category(slug=f"many-{i}")
            make_tool(slug=f"tool-{i}", categories=[cat])
        cache.clear()
        html = self.client.get("/en/").content.decode()
        footer = html[html.find("<!-- Footer -->"):]
        cat_start = footer.find('id="footer-categories-heading"')
        cat_end = footer.find("</nav>", cat_start)
        cat_html = footer[cat_start:cat_end]
        self.assertLessEqual(cat_html.count("<li>"), 6)

    def test_category_link_uses_the_real_catalog_query_param_filter(self):
        cat = make_category(slug="filter-me")
        make_tool(slug="t1", categories=[cat])
        cache.clear()
        html = self.client.get("/en/").content.decode()
        self.assertIn(f'{reverse("catalog:list")}?category=filter-me-en', html)

    def test_no_popularity_language_used(self):
        cat = make_category(slug="neutral")
        make_tool(slug="t1", categories=[cat])
        cache.clear()
        html = self.client.get("/en/").content.decode()
        footer = html[html.find("<!-- Footer -->"):]
        for banned in ("Popular", "Trending", "Most popular", "Trusted by"):
            self.assertNotIn(banned, footer)


class FooterStarterTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_starter_link_present_for_explicit_starter(self):
        starter = make_guide(slug="footer-starter-arbitrary-slug", is_starter=True)
        cache.clear()
        html = self.client.get("/en/").content.decode()
        footer = html[html.find("<!-- Footer -->"):]
        self.assertIn(starter.get_absolute_url(language="en"), footer)

    def test_no_starter_link_when_none_published(self):
        html = self.client.get("/en/").content.decode()
        footer = html[html.find("<!-- Footer -->"):]
        starter_start = footer.find('id="footer-starter-heading"')
        starter_end = footer.find("</nav>", starter_start)
        starter_html = footer[starter_start:starter_end]
        self.assertNotIn("guides:detail", starter_html)
        self.assertNotIn("/en/guides/", starter_html)

    def test_starter_works_regardless_of_slug(self):
        starter = make_guide(slug="totally-unrelated-name", is_starter=True)
        cache.clear()
        html = self.client.get("/en/").content.decode()
        self.assertIn(starter.get_absolute_url(language="en"), html)


class FooterPreservedFunctionsTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_github_link_present(self):
        html = self.client.get("/en/").content.decode()
        self.assertIn("https://github.com/maikksmt/mentoro-ai", html)

    def test_newsletter_link_present(self):
        html = self.client.get("/en/").content.decode()
        self.assertIn(reverse("newsletter:subscribe"), html)

    def test_consent_settings_trigger_present(self):
        html = self.client.get("/en/").content.decode()
        self.assertIn("klaro.show(klaroConfig, true)", html)

    def test_legal_links_present(self):
        html = self.client.get("/en/").content.decode()
        self.assertIn(reverse("legal:legal-notice"), html)
        self.assertIn(reverse("legal:privacy"), html)
        self.assertIn(reverse("legal:cookies"), html)
        self.assertIn(reverse("legal:terms-of-use"), html)
        self.assertIn(reverse("legal:copyright"), html)

    def test_copyright_present(self):
        html = self.client.get("/en/").content.decode()
        self.assertIn("MentoroAI", html)
        self.assertIn("©", html)

    def test_what_to_find_link_preserved_in_start_here_group(self):
        html = self.client.get("/en/").content.decode()
        footer = html[html.find("<!-- Footer -->"):]
        self.assertIn(reverse("what-to-find"), footer)
