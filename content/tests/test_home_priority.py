"""
Beta 9.7: the homepage is reprioritized for returning users.

Before, the section a returning user actually comes for ("Current content")
sat below the entry cards, the author block and the featured tools - roughly
two viewport heights down on a 1440x900 laptop and 4.3 on a phone. This
locks in the new order and the entry-card corrections that came with it
(no nested <button> inside the card link, no role="button"/aria-pressed on
plain links, one shared structure including Glossary, and a visually
recessed state for areas whose public_inventory count is 0).

Deliberately asserts on DOM order and the stable `.home-*` structure
classes rather than on full Tailwind class strings, and never on the
queries themselves - those are unchanged in this slice and covered by
test_home_inventory / test_latest_content / test_home_starter.
"""
import re
from html.parser import HTMLParser

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone, translation

from catalog.models import Category, Tool
from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin
from guides.models import Guide

ENTRY_CARD_RE = re.compile(r'<a[^>]*class="home-entry-card[^"]*"[^>]*>.*?</a>', re.DOTALL)


class _NestingChecker(HTMLParser):
    """Detects genuine nesting (not merely sibling tags) of a given tag."""

    def __init__(self, tag):
        super().__init__()
        self.tag = tag
        self.depth = 0
        self.found_nested = False

    def handle_starttag(self, tag, attrs):
        if tag == self.tag:
            if self.depth:
                self.found_nested = True
            self.depth += 1

    def handle_endtag(self, tag):
        if tag == self.tag and self.depth:
            self.depth -= 1


def _has_nested(html, tag):
    checker = _NestingChecker(tag)
    checker.feed(html)
    return checker.found_nested


class HomeSectionOrderTests(TestCase):
    """The returning-user content must come before discovery and trust blocks."""

    @classmethod
    def setUpTestData(cls):
        with translation.override("en"):
            cls.guide = Guide.objects.create(
                status=EditorialWorkflowMixin.STATUS_PUBLISHED,
                published_at=timezone.now(),
                is_starter=True,
            )
            cls.guide.create_translation(
                "en", slug="hp-guide-en", title="HP Guide", intro="i", body="b"
            )

    def setUp(self):
        cache.clear()

    def _html(self):
        resp = self.client.get("/en/")
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_current_content_comes_before_entry_cards(self):
        html = self._html()
        self.assertLess(
            html.index("Current content"),
            html.index("What can I find on MentoroAI?"),
            "Current content must be rendered before the discovery entry cards",
        )

    def test_current_content_comes_before_author_block(self):
        html = self._html()
        self.assertLess(
            html.index("Current content"),
            html.index("Become an author on MentoroAI"),
        )

    def test_entry_cards_come_before_author_block(self):
        html = self._html()
        self.assertLess(
            html.index("What can I find on MentoroAI?"),
            html.index("Become an author on MentoroAI"),
        )

    def test_author_block_is_still_present(self):
        self.assertIn("Become an author on MentoroAI", self._html())

    def test_hero_still_offers_the_starter_cta(self):
        html = self._html()
        self.assertIn(self.guide.get_absolute_url(language="en"), html)
        self.assertIn("Begin with the Starter Guide", html)
        self.assertNotIn('href=""', html)

    def test_hero_is_not_viewport_height_bound(self):
        """The old `hero min-h-[40vh]` grew the hero with the viewport; the
        replacement uses fixed responsive padding instead."""
        html = self._html()
        self.assertIn("home-hero", html)
        self.assertNotIn("min-h-[40vh]", html)

    def test_featured_tools_section_still_renders_when_tools_exist(self):
        tool = Tool.objects.create(
            slug="hp-featured", published_at=timezone.now(), is_featured=True
        )
        with translation.override("en"):
            tool.create_translation("en", name="HP Featured Tool", short_description="s")
        cache.clear()
        html = self._html()
        self.assertIn("Featured Tools", html)
        self.assertIn("HP Featured Tool", html)


class HomeEntryCardStructureTests(TestCase):
    """All six entry cards share one corrected structure."""

    def setUp(self):
        cache.clear()

    def _cards(self):
        resp = self.client.get("/en/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        cards = ENTRY_CARD_RE.findall(html)
        self.assertEqual(len(cards), 6, "expected exactly six entry cards")
        return cards

    def test_no_nested_button_inside_the_card_link(self):
        """Every card used to be <a ...><button>...</button></a> - invalid
        interactive nesting on all six."""
        for card in self._cards():
            with self.subTest(card=card[:80]):
                self.assertNotIn("<button", card)

    def test_no_nested_link_inside_the_card_link(self):
        for card in self._cards():
            with self.subTest(card=card[:80]):
                self.assertFalse(_has_nested(card, "a"))

    def test_no_role_button_or_aria_pressed_on_entry_links(self):
        for card in self._cards():
            with self.subTest(card=card[:80]):
                self.assertNotIn('role="button"', card)
                self.assertNotIn("aria-pressed", card)

    def test_every_card_keeps_an_accessible_link_name(self):
        for card in self._cards():
            with self.subTest(card=card[:80]):
                self.assertIn("aria-label=", card)

    def test_glossary_card_uses_the_same_structure_class_as_the_others(self):
        """Glossary was the odd one out: no bg class and no count line, which
        made it visibly shorter than its five siblings."""
        cards = self._cards()
        glossary = next(c for c in cards if "Go to Glossary list" in c)
        for marker in ("home-entry-card", "home-entry-card-body",
                       "home-entry-card-title", "home-entry-card-action"):
            self.assertIn(marker, glossary)

    def test_glossary_card_still_has_no_invented_count(self):
        """public_inventory has no glossary count and this slice must not
        invent one - the shared structure has to absorb the gap instead.
        Glossary therefore carries only its description paragraph, while
        every counted area carries description + count."""
        cards = self._cards()
        glossary = next(c for c in cards if "Go to Glossary list" in c)
        guides = next(c for c in cards if "Go to Guides list" in c)

        # `<p` alone also matches `<path` (Beta 9.9 added a heroicon SVG,
        # which is a `<path>` element, to the card title) - match only real
        # `<p>` tag openings instead.
        def count_p(html):
            return len(re.findall(r"<p[ >]", html))

        self.assertEqual(count_p(glossary), 1, "glossary must have no count line")
        self.assertEqual(count_p(guides), 2, "counted areas keep their count line")


class HomeEmptyAreaTests(TestCase):
    """An area reporting 0 must not be advertised like a stocked one."""

    def setUp(self):
        cache.clear()

    def _cards(self):
        html = self.client.get("/en/").content.decode()
        return ENTRY_CARD_RE.findall(html)

    def test_zero_count_area_is_visually_recessed(self):
        # No comparisons exist in this test database.
        comparisons = next(c for c in self._cards() if "Go to Comparisons list" in c)
        self.assertIn("0 comparisons", comparisons)
        self.assertIn("home-entry-card-muted", comparisons)

    def test_stocked_area_is_not_recessed(self):
        with translation.override("en"):
            guide = Guide.objects.create(
                status=EditorialWorkflowMixin.STATUS_PUBLISHED,
                published_at=timezone.now(),
            )
            guide.create_translation(
                "en", slug="stocked-en", title="Stocked", intro="i", body="b"
            )
        cache.clear()
        guides = next(c for c in self._cards() if "Go to Guides list" in c)
        self.assertIn("1 guide", guides)
        self.assertNotIn("home-entry-card-muted", guides)

    def test_recession_is_generic_not_hardcoded_to_comparisons(self):
        """Any area at 0 gets the muted treatment - it is driven by the
        existing count, not by a per-area special case."""
        cards = self._cards()
        # Fresh DB: guides, prompts, use cases and comparisons are all 0.
        for label in ("Go to Guides list", "Go to Prompts list",
                      "Go to Usecases list", "Go to Comparisons list"):
            with self.subTest(label=label):
                card = next(c for c in cards if label in c)
                self.assertIn("home-entry-card-muted", card)

    def test_zero_area_is_still_reachable(self):
        """Recessed, not removed: the link must still resolve."""
        comparisons = next(c for c in self._cards() if "Go to Comparisons list" in c)
        href = re.search(r'href="([^"]+)"', comparisons).group(1)
        self.assertEqual(self.client.get(href).status_code, 200)

    def test_zero_state_is_not_conveyed_by_colour_alone(self):
        """The visible "0 ..." count carries the meaning; the muted surface
        only reinforces it."""
        comparisons = next(c for c in self._cards() if "Go to Comparisons list" in c)
        self.assertIn("0 comparisons", comparisons)


class HomeInventoryStillDynamicTests(TestCase):
    """The reprioritization must not have frozen any number into the markup."""

    def setUp(self):
        cache.clear()

    def test_counts_follow_real_inventory(self):
        html = self.client.get("/en/").content.decode()
        self.assertIn("0 guides", html)

        with translation.override("en"):
            g = Guide.objects.create(
                status=EditorialWorkflowMixin.STATUS_PUBLISHED,
                published_at=timezone.now(),
            )
            g.create_translation("en", slug="dyn-en", title="Dyn", intro="i", body="b")
        cache.clear()

        html = self.client.get("/en/").content.decode()
        self.assertIn("1 guide<", html)

    def test_comparison_count_reflects_real_data(self):
        html = self.client.get("/en/").content.decode()
        self.assertIn("0 comparisons", html)

        c = Comparison.objects.create(
            status=EditorialWorkflowMixin.STATUS_PUBLISHED, published_at=timezone.now()
        )
        c.create_translation("en", title="C", intro="i", body="b", slug="dyn-cmp-en")
        cache.clear()

        html = self.client.get("/en/").content.decode()
        self.assertIn("1 comparison<", html)
        cards = ENTRY_CARD_RE.findall(html)
        comparisons_card = next(x for x in cards if "Go to Comparisons list" in x)
        self.assertNotIn("home-entry-card-muted", comparisons_card)


class HomeLinkIntegrityTests(TestCase):
    """Every link the reordered homepage renders must still resolve."""

    def setUp(self):
        cache.clear()

    def test_all_internal_homepage_links_return_200(self):
        cat = Category.objects.create()
        with translation.override("en"):
            cat.create_translation("en", name="Cat", slug="hp-cat-en")
            tool = Tool.objects.create(
                slug="hp-tool", published_at=timezone.now(), is_featured=True
            )
            tool.create_translation("en", name="HP Tool", short_description="s")
            tool.categories.add(cat)
        cache.clear()

        html = self.client.get("/en/").content.decode()
        hrefs = {
            h.split("#", 1)[0]
            for h in re.findall(r'href="([^"]+)"', html)
            if h.startswith("/")
            and not h.startswith(("/admin", "/static", "/media", "/accounts"))
        }
        self.assertTrue(hrefs)
        for href in sorted(hrefs):
            if not href:
                continue
            with self.subTest(href=href):
                self.assertEqual(self.client.get(href).status_code, 200)
