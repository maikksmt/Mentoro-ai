from unittest.mock import patch

from django.core.cache import cache
from django.db import connection
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import resolve, reverse
from django.utils import translation

from core.context_processors import nav_active_section, public_inventory
from mentoroai.settings import TEMPLATES


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


class PublicInventoryContextProcessorTests(TestCase):
    """Beta 8.7: the public_inventory context processor is a thin wrapper
    around core.services.get_public_inventory - no query logic of its own."""

    def setUp(self):
        cache.clear()

    def test_registered_in_template_settings(self):
        processors = TEMPLATES[0]["OPTIONS"]["context_processors"]
        self.assertIn("core.context_processors.public_inventory", processors)

    def test_context_key_is_public_inventory(self):
        request = RequestFactory().get("/")
        ctx = public_inventory(request)
        self.assertIn("public_inventory", ctx)
        self.assertIn("counts", ctx["public_inventory"])

    def test_uses_the_current_active_language(self):
        request = RequestFactory().get("/")
        with translation.override("de"):
            with patch("core.context_processors.get_public_inventory") as mocked:
                mocked.return_value = {"counts": {}, "top_categories": [], "starter_guide": None}
                public_inventory(request)
        mocked.assert_called_once_with("de")

    def test_delegates_to_the_shared_service_only(self):
        request = RequestFactory().get("/")
        with patch("core.context_processors.get_public_inventory") as mocked:
            mocked.return_value = {"sentinel": True}
            result = public_inventory(request)
        mocked.assert_called_once()
        self.assertEqual(result, {"public_inventory": {"sentinel": True}})

    def test_cache_hit_causes_no_inventory_queries_in_the_context_processor(self):
        from core.services import get_public_inventory
        get_public_inventory("en")  # warm the cache

        request = RequestFactory().get("/")
        with translation.override("en"):
            with CaptureQueriesContext(connection) as ctx:
                public_inventory(request)
        inventory_tables = ("catalog_tool", "catalog_category", "guides_guide",
                            "prompts_prompt", "usecases_usecase", "compare_comparison")
        touched = [q for q in ctx.captured_queries if any(t in q["sql"] for t in inventory_tables)]
        self.assertEqual(touched, [])

    def test_other_context_processors_still_registered(self):
        processors = TEMPLATES[0]["OPTIONS"]["context_processors"]
        self.assertIn("core.context_processors.nav_active_section", processors)
        self.assertIn("core.context_processors.account_signup_settings", processors)
        self.assertIn("django.template.context_processors.request", processors)

    def test_available_in_a_rendered_public_template(self):
        resp = self.client.get(reverse("content:home"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("public_inventory", resp.context)
        self.assertIn("counts", resp.context["public_inventory"])
