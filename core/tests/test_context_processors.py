from django.test import RequestFactory, TestCase
from django.urls import resolve, reverse
from django.utils import translation

from core.context_processors import nav_active_section


class NavActiveSectionContextProcessorTests(TestCase):
    """Beta 8.2: the active primary nav section is derived from
    request.resolver_match, not from fragile request.path substrings."""

    def _section_for(self, url_name, **kwargs):
        with translation.override("en"):
            path = reverse(url_name, kwargs=kwargs or None)
        request = RequestFactory().get(path)
        request.resolver_match = resolve(path)
        return nav_active_section(request)["nav_active_section"]

    def test_no_resolver_match_returns_none(self):
        request = RequestFactory().get("/")
        request.resolver_match = None
        self.assertIsNone(nav_active_section(request)["nav_active_section"])

    def test_home_page_marks_home(self):
        self.assertEqual(self._section_for("content:home"), "home")

    def test_catalog_list_and_detail_mark_catalog(self):
        self.assertEqual(self._section_for("catalog:list"), "catalog")
        self.assertEqual(self._section_for("catalog:detail", slug="x"), "catalog")

    def test_guides_list_and_detail_mark_guides(self):
        self.assertEqual(self._section_for("guides:list"), "guides")
        self.assertEqual(self._section_for("guides:detail", slug="x"), "guides")

    def test_prompts_list_and_detail_mark_prompts(self):
        self.assertEqual(self._section_for("prompts:list"), "prompts")
        self.assertEqual(self._section_for("prompts:detail", slug="x"), "prompts")

    def test_usecases_list_and_detail_mark_usecases(self):
        self.assertEqual(self._section_for("usecases:list"), "usecases")
        self.assertEqual(self._section_for("usecases:detail", slug="x"), "usecases")

    def test_compare_index_and_detail_mark_compare(self):
        self.assertEqual(self._section_for("compare:index"), "compare")
        self.assertEqual(self._section_for("compare:detail", slug="x"), "compare")

    def test_glossary_list_and_detail_mark_glossary(self):
        self.assertEqual(self._section_for("glossary:list"), "glossary")
        self.assertEqual(self._section_for("glossary:detail", slug="x"), "glossary")

    def test_legal_pages_mark_no_section(self):
        self.assertIsNone(self._section_for("legal:privacy"))
        self.assertIsNone(self._section_for("legal:legal-notice"))

    def test_newsletter_marks_no_section(self):
        self.assertIsNone(self._section_for("newsletter:subscribe"))

    def test_account_dashboard_marks_no_section(self):
        self.assertIsNone(self._section_for("account_dashboard"))

    def test_editorial_subpage_does_not_mark_home(self):
        self.assertIsNone(self._section_for("content:editorial:layout_examples"))
