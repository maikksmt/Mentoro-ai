"""
Beta 9.6: guide/prompt/usecase/comparison list cards, the homepage "latest
content" teasers, and every related-content section were consolidated from
two near-duplicate partials (_teaser_card.html/_guideitem_card.html) plus
four inline card blocks into one shared templates/partials/_editorial_card.html.

These tests pin the audit findings the slice was meant to fix: no nested
interactive elements, no role="button"/aria-pressed misuse on plain links,
the shared `.editorial-card` structure is actually used everywhere, and
type-specific information (starter badge, persona, involved tools) survives
consolidation. Deliberately avoids asserting on exact Tailwind class
strings - only on the stable `.editorial-card*` semantic classes and on
content/attributes that matter for correctness and accessibility.
"""
import re
from html.parser import HTMLParser

from django.test import TestCase
from django.utils import timezone, translation
from parler.utils.context import switch_language

from catalog.models import Tool
from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin
from guides.models import Guide
from prompts.models import Prompt
from usecases.models import UseCase

CARD_RE = re.compile(r'<article class="editorial-card[^"]*">.*?</article>', re.DOTALL)


def _extract_cards(html):
    return CARD_RE.findall(html)


class _NestingChecker(HTMLParser):
    """Tracks open-tag depth to find real (not merely sibling) nesting."""

    def __init__(self, *tags):
        super().__init__()
        self.tags = tags
        self.stack = []
        self.found_nested = {t: False for t in tags}

    def handle_starttag(self, tag, attrs):
        if tag in self.tags:
            if tag in self.stack:
                self.found_nested[tag] = True
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.tags and tag in self.stack:
            # pop back to (and including) the matching open tag
            while self.stack and self.stack.pop() != tag:
                pass


def _has_real_nesting(html, *tags):
    checker = _NestingChecker(*tags)
    checker.feed(html)
    return any(checker.found_nested.values())


class EditorialCardStructureTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # Several of these managers (Prompt.objects.create() with translated
        # kwargs, UseCase.published.create()) resolve translations against
        # Django's *ambient* active language when called outside a request -
        # explicit override keeps this fixture order-independent regardless
        # of what earlier tests in the same process left active (see
        # prompts.tests.test_templates for the same documented pattern).
        with translation.override("en"):
            cls.tool = Tool.objects.create(slug="ec-tool", published_at=timezone.now())
            cls.tool.create_translation("en", name="EC Tool")

            cls.guide = Guide.objects.create(
                status=EditorialWorkflowMixin.STATUS_PUBLISHED,
                published_at=timezone.now(),
                is_starter=True,
            )
            cls.guide.create_translation("en", slug="ec-guide-en", title="EC Guide", intro="Guide intro", body="b")

            cls.prompt = Prompt.objects.create(
                title="EC Prompt", slug="ec-prompt-en", body="Prompt body",
                status="published", published_at=timezone.now(),
            )

            cls.usecase = UseCase.published.create(
                slug="ec-usecase-en", status=EditorialWorkflowMixin.STATUS_PUBLISHED,
                published_at=timezone.now(),
            )
            with switch_language(cls.usecase, "en"):
                cls.usecase.title = "EC Usecase"
                cls.usecase.intro = "Usecase intro"
                cls.usecase.persona = "Busy freelancer"
                cls.usecase.save()

            cls.comparison = Comparison.objects.create(
                status=EditorialWorkflowMixin.STATUS_PUBLISHED, published_at=timezone.now()
            )
            cls.comparison.create_translation(
                "en", title="EC Comparison", intro="Comparison intro", body="b", slug="ec-comparison-en"
            )
            cls.comparison.tools.add(cls.tool)

    def _cards_on(self, path):
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200, path)
        html = resp.content.decode()
        cards = _extract_cards(html)
        self.assertTrue(cards, f"no .editorial-card found on {path}")
        return cards

    def test_list_pages_render_the_shared_card_structure(self):
        for path in ("/en/guides/", "/en/prompts/", "/en/usecases/", "/en/compare/"):
            with self.subTest(path=path):
                self._cards_on(path)

    def test_homepage_latest_content_renders_the_shared_card_structure(self):
        self._cards_on("/en/")

    def test_no_nested_interactive_elements_in_any_card(self):
        for path in ("/en/guides/", "/en/prompts/", "/en/usecases/", "/en/compare/", "/en/"):
            for card in self._cards_on(path):
                with self.subTest(path=path):
                    self.assertNotIn("<button", card)
                    self.assertFalse(
                        _has_real_nesting(card, "a"),
                        "an <a> contains another <a> inside a card",
                    )

    def test_no_role_button_or_aria_pressed_on_card_links(self):
        for path in ("/en/guides/", "/en/prompts/", "/en/usecases/", "/en/compare/", "/en/"):
            for card in self._cards_on(path):
                with self.subTest(path=path):
                    self.assertNotIn("role=\"button\"", card)
                    self.assertNotIn("aria-pressed", card)

    def test_title_and_cta_links_have_distinct_accessible_names(self):
        html = self.client.get("/en/guides/").content.decode()
        card = _extract_cards(html)[0]
        # The title link's own text is its accessible name (no aria-label
        # override); the CTA link's visible text is "Read more", not a
        # duplicate "Read details about <title>" - this is the redundant-
        # link-name pattern the slice removed.
        self.assertNotIn("Read details about", card)
        self.assertIn("Read more", card)

    def test_no_forced_ellipsis_when_content_is_not_actually_truncated(self):
        html = self.client.get("/en/prompts/").content.decode()
        card = next(c for c in _extract_cards(html) if "EC Prompt" in c)
        self.assertNotIn("Prompt body ...", card)
        self.assertNotIn("Prompt body...", card)

    def test_starter_badge_survives_on_the_guide_card(self):
        html = self.client.get("/en/guides/").content.decode()
        card = next(c for c in _extract_cards(html) if "EC Guide" in c)
        self.assertIn("Start here", card)

    def test_persona_survives_on_the_usecase_card(self):
        html = self.client.get("/en/usecases/").content.decode()
        card = next(c for c in _extract_cards(html) if "EC Usecase" in c)
        self.assertIn("Busy freelancer", card)

    def test_involved_tools_survive_on_the_comparison_card(self):
        html = self.client.get("/en/compare/").content.decode()
        card = next(c for c in _extract_cards(html) if "EC Comparison" in c)
        self.assertIn("EC Tool", card)
        self.assertIn(f'/en/catalog/{self.tool.slug}/', card)

    def test_tool_catalog_cards_are_unaffected(self):
        """Tool cards are an explicitly separate card family and must not
        pick up the new .editorial-card class."""
        html = self.client.get("/en/catalog/").content.decode()
        self.assertNotIn("editorial-card", html)
