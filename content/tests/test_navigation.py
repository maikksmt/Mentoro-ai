import re

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone, translation
from parler.utils.context import switch_language

from catalog.models import Tool
from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin
from glossary.models import GlossaryTerm
from guides.models import Guide
from prompts.models import Prompt
from usecases.models import UseCase

User = get_user_model()

DEAD_DRAWER_MARKERS = ("mobile-drawer", "menu-open", "menu-close", "mobile-backdrop")


def _get(url_name, **kwargs):
    with translation.override("en"):
        url = reverse(url_name, kwargs=kwargs or None)
    return url


class DeadDrawerCodeTests(TestCase):
    """Beta 8.2: the unused DaisyUI-less drawer JS/markup must be gone."""

    def test_home_page_has_no_drawer_references(self):
        resp = self.client.get(_get("content:home"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        for marker in DEAD_DRAWER_MARKERS:
            self.assertNotIn(marker, html)


class MobileDropdownStructureTests(TestCase):
    """Beta 8.2: the DaisyUI dropdown is hardened as an accessible disclosure."""

    def setUp(self):
        resp = self.client.get(_get("content:home"))
        self.assertEqual(resp.status_code, 200)
        self.html = resp.content.decode()

    def test_trigger_is_a_button_with_stable_id(self):
        self.assertIn('<button type="button" id="mobile-nav-trigger"', self.html)

    def test_trigger_has_accessible_label(self):
        self.assertIn('id="mobile-nav-trigger"', self.html)
        self.assertIn("aria-label=", self.html)

    def test_trigger_is_connected_to_menu_via_aria_controls(self):
        self.assertIn('aria-controls="mobile-nav-menu"', self.html)
        self.assertIn('id="mobile-nav-menu"', self.html)

    def test_trigger_starts_collapsed(self):
        self.assertIn('aria-expanded="false"', self.html)

    def test_menu_contains_plain_links_without_menu_roles(self):
        self.assertIn('id="mobile-nav-menu"', self.html)
        self.assertNotIn('role="menu"', self.html)
        self.assertNotIn('role="menuitem"', self.html)

    def test_scrim_present_and_marked_decorative(self):
        """
        Beta 9.5: the mobile dropdown gained a dedicated scrim element so it
        reads as its own surface over the dimmed page, rather than floating
        directly over unmodified content. It carries no interactive role of
        its own - the existing outside-click handler on the page already
        closes the menu when the scrim (a sibling of the dropdown) is
        clicked.
        """
        self.assertIn('id="mobile-nav-scrim"', self.html)
        self.assertIn('aria-hidden="true"', self.html)


class ActiveNavSectionTests(TestCase):
    """Beta 8.2: aria-current/active state uses request.resolver_match, not path strings."""

    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user(
            username="nav-author", email="nav-author@example.com", password="testpass123"
        )

        cls.tool = Tool.objects.create(slug="nav-tool", website="https://example.com/nav-tool")
        with switch_language(cls.tool, "en"):
            cls.tool.name = "Nav Tool"
            cls.tool.short_description = "S"
            cls.tool.long_description = "L"
            cls.tool.save()

        cls.guide = Guide.objects.create(
            status=EditorialWorkflowMixin.STATUS_PUBLISHED, published_at=timezone.now()
        )
        cls.guide.create_translation("en", slug="nav-guide", title="Nav Guide", intro="i", body="b")

        cls.prompt = Prompt.objects.create(
            title="Nav Prompt", slug="nav-prompt", body="Body",
            status="published", published_at=timezone.now(),
        )

        cls.usecase = UseCase.published.create(
            slug="nav-usecase", status=EditorialWorkflowMixin.STATUS_PUBLISHED,
            published_at=timezone.now(), author=cls.author,
        )
        with switch_language(cls.usecase, "en"):
            cls.usecase.title = "Nav Usecase"
            cls.usecase.intro = "Intro"
            cls.usecase.save()

        cls.comparison = Comparison.objects.create(
            status=EditorialWorkflowMixin.STATUS_PUBLISHED, published_at=timezone.now()
        )
        cls.comparison.create_translation("en", slug="nav-comparison", title="Nav Comparison", intro="i", body="b")

        cls.term = GlossaryTerm.objects.create(
            term="Nav Term", slug="nav-term", short_definition="short",
            long_definition="long", category="General", language="en",
            created_at=timezone.now(), updated_at=timezone.now(),
        )

    def _assert_only_section_active(self, url, section, expected_count=2):
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()

        all_sections = ("catalog", "guides", "prompts", "usecases", "compare", "glossary")
        self.assertIn(section, all_sections)

        # btn-active is exclusive to nav links (breadcrumbs use aria-current
        # separately for the current page name, so we anchor on this class
        # rather than counting aria-current across the whole document).
        self.assertEqual(html.count("btn-active"), expected_count, html)
        active_links = re.findall(r'aria-current="page"[^>]*btn-active', html)
        self.assertEqual(len(active_links), expected_count, html)

    def test_catalog_list_marks_catalog(self):
        self._assert_only_section_active(_get("catalog:list"), "catalog")

    def test_catalog_detail_marks_catalog(self):
        self._assert_only_section_active(_get("catalog:detail", slug=self.tool.slug), "catalog")

    def test_guides_list_marks_guides(self):
        self._assert_only_section_active(_get("guides:list"), "guides")

    def test_guides_detail_marks_guides(self):
        self._assert_only_section_active(_get("guides:detail", slug="nav-guide"), "guides")

    def test_prompts_list_marks_prompts(self):
        self._assert_only_section_active(_get("prompts:list"), "prompts")

    def test_prompts_detail_marks_prompts(self):
        self._assert_only_section_active(_get("prompts:detail", slug=self.prompt.slug), "prompts")

    def test_usecases_list_marks_usecases(self):
        self._assert_only_section_active(_get("usecases:list"), "usecases")

    def test_usecases_detail_marks_usecases(self):
        self._assert_only_section_active(_get("usecases:detail", slug="nav-usecase"), "usecases")

    def test_comparisons_list_marks_compare(self):
        self._assert_only_section_active(_get("compare:index"), "compare")

    def test_comparisons_detail_marks_compare(self):
        self._assert_only_section_active(_get("compare:detail", slug="nav-comparison"), "compare")

    def test_glossary_list_marks_glossary(self):
        self._assert_only_section_active(_get("glossary:list"), "glossary")

    def test_glossary_detail_marks_glossary(self):
        self._assert_only_section_active(_get("glossary:detail", slug="nav-term"), "glossary")

    def test_legal_page_does_not_mark_home(self):
        resp = self.client.get(_get("legal:privacy"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertNotIn('aria-current="page"', html)
        self.assertNotIn("btn-active", html)

    def test_desktop_and_mobile_nav_agree_on_active_section(self):
        resp = self.client.get(_get("guides:list"))
        html = resp.content.decode()

        mobile_start = html.index('id="mobile-nav-menu"')
        desktop_start = html.index('class="menu menu-horizontal')
        mobile_snippet = html[mobile_start:desktop_start]
        desktop_snippet = html[desktop_start:]

        self.assertIn("btn-active", mobile_snippet)
        self.assertIn("btn-active", desktop_snippet)
