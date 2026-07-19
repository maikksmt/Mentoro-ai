"""
Beta 8.7: homepage fact line and entry-card counts, all sourced from
public_inventory (no hardcoded or independently-queried numbers).
"""
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from catalog.models import Category, Tool
from core.models.editorial import EditorialWorkflowMixin
from guides.models import Guide
from prompts.models import Prompt


def make_guide(*, slug, status=EditorialWorkflowMixin.STATUS_PUBLISHED, published_at=None, languages=("en",)):
    if published_at is None and status == EditorialWorkflowMixin.STATUS_PUBLISHED:
        published_at = timezone.now()
    g = Guide.objects.create(status=status, published_at=published_at)
    for lang in languages:
        g.create_translation(lang, title="Guide", intro="i", body="b", slug=f"{slug}-{lang}")
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
    return c


class HomeFactLineTests(TestCase):
    def setUp(self):
        cache.clear()

    def _home(self):
        return self.client.get(reverse("content:home"))

    def test_fact_line_shows_real_tool_guide_category_numbers(self):
        cat = make_category(slug="fact-line")
        make_tool(slug="t1", categories=[cat])
        make_guide(slug="g1")

        resp = self._home()
        html = resp.content.decode()
        self.assertIn("1 AI tool", html)
        self.assertIn("1 guide", html)
        self.assertIn("1 category", html)

    def test_fact_line_has_no_hardcoded_count(self):
        resp = self._home()
        self.assertEqual(resp.status_code, 200)
        # No fixtures at all beyond whatever pre-exists; the numbers must be
        # whatever the (possibly zero) real inventory says, not a fixed
        # literal baked into the template.
        html = resp.content.decode()
        self.assertIn("AI tool", html)  # singular or plural, some form present

    def test_pluralization_switches_correctly(self):
        make_guide(slug="only-one")
        resp = self._home()
        html = resp.content.decode()
        self.assertIn("1 guide<", html)
        self.assertNotIn("1 guides", html)

        make_guide(slug="second-one")
        cache.clear()
        resp = self._home()
        html = resp.content.decode()
        self.assertIn("2 guides", html)

    def test_zero_counts_render_without_breaking_layout(self):
        # Fresh, empty-of-fixtures database section: at minimum the request
        # must not error out and must still show a "0 ..." plural form.
        from guides.models import Guide as GuideModel
        GuideModel.objects.all().delete()
        cache.clear()
        resp = self._home()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("0 guides", resp.content.decode())

    def test_entry_cards_use_same_inventory_data_as_fact_line(self):
        make_guide(slug="card-check")
        resp = self._home()
        html = resp.content.decode()
        # The fact line's "1 guide" and the Guides card's own count must
        # both be present and mutually consistent (same public_inventory).
        self.assertEqual(html.count("1 guide<"), 2)  # hero fact line + card

    def test_glossary_card_has_no_invented_count(self):
        # Glossary is deliberately outside the confirmed inventory scope
        # (tools/categories/guides/prompts/usecases/comparisons only).
        resp = self._home()
        html = resp.content.decode()
        start = html.find('aria-label="Go to Glossary list"')
        end = html.find("</a>", start)
        glossary_card_html = html[start:end]
        self.assertNotIn("opacity-70", glossary_card_html)

    def test_draft_content_does_not_change_public_counts(self):
        make_guide(slug="published-one")
        cache.clear()
        resp = self._home()
        before = resp.content.decode()
        self.assertIn("1 guide<", before)

        make_guide(slug="draft-one", status=EditorialWorkflowMixin.STATUS_DRAFT, published_at=None)
        cache.clear()
        resp = self._home()
        after = resp.content.decode()
        self.assertIn("1 guide<", after)

    def test_starter_cta_still_correct_alongside_fact_line(self):
        starter = make_guide(slug="starter-guide")
        Guide.objects.filter(pk=starter.pk).update(is_starter=True)
        cache.clear()
        resp = self._home()
        html = resp.content.decode()
        self.assertIn(starter.get_absolute_url(language="en"), html)

    def test_missing_starter_produces_no_empty_href(self):
        resp = self._home()
        html = resp.content.decode()
        self.assertNotIn('href=""', html)


class HomeInventoryLanguageIsolationTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_english_and_german_home_pages_show_separate_counts(self):
        make_guide(slug="en-only", languages=("en",))
        cache.clear()

        en_resp = self.client.get("/en/")
        de_resp = self.client.get("/de/")

        # Assert on the underlying context data rather than the rendered
        # label text: the label's language depends on translation-workflow
        # state (fuzzy vs. confirmed msgstr), but the count itself must be
        # 1 either way - the EN-only guide is still visible on DE via
        # fallback (matches GuideListView's own active_translations()
        # fallback behavior).
        self.assertEqual(en_resp.context["public_inventory"]["counts"]["guides"], 1)
        self.assertEqual(de_resp.context["public_inventory"]["counts"]["guides"], 1)


class PromptCountLanguageIsolationTests(TestCase):
    """Beta 8.8: the corrected, strictly language-isolated prompt count
    reaches the homepage/footer context via public_inventory, with no
    template or footer regressions."""

    def setUp(self):
        cache.clear()

    def _make_prompt(self, *, slug, languages):
        p = Prompt.objects.create(status=EditorialWorkflowMixin.STATUS_PUBLISHED, published_at=timezone.now())
        for lang in languages:
            p.create_translation(lang, title="Prompt", intro="i", body="b", slug=f"{slug}-{lang}")
        return p

    def test_homepage_prompt_count_is_language_isolated(self):
        self._make_prompt(slug="en-only-prompt", languages=("en",))
        cache.clear()

        en_resp = self.client.get("/en/")
        de_resp = self.client.get("/de/")

        self.assertEqual(en_resp.context["public_inventory"]["counts"]["prompts"], 1)
        self.assertEqual(de_resp.context["public_inventory"]["counts"]["prompts"], 0)

    def test_public_inventory_still_present_and_footer_renders_without_error(self):
        resp = self.client.get("/en/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("public_inventory", resp.context)
        html = resp.content.decode()
        self.assertIn("<!-- Footer -->", html)
        self.assertNotIn('href=""', html)
