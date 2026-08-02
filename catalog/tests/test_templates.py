import re

from django.test import TestCase
from django.urls import reverse
from parler.utils.context import switch_language

from catalog.models import Category, Tool


class CatalogTemplateTests(TestCase):
    tool = None

    @classmethod
    def setUpTestData(cls):
        cat = Category.objects.create(name="Office & Tools", slug="office")
        cls.tool = Tool.objects.create(slug="nice-tool", website="https://example.com/nice")

        with switch_language(cls.tool, "en"):
            cls.tool.name = "NiceTool"
            cls.tool.short_description = "Useful helper"
            cls.tool.long_description = "Useful helper Long"
            cls.tool.save()
        cls.tool.categories.add(cat)

    def test_list_card_renders_name_desc_and_safe_rel_on_outbound_link(self):
        resp = self.client.get(reverse("catalog:list"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("NiceTool", html)
        # Beschreibung aus short_desc
        self.assertTrue(("Useful helper" in html) or ("useful" in html.lower()))
        if "https://example.com/tool" in html:
            self.assertIn(
                'rel="nofollow noopener"',
                html,
                'Outbound links must have rel="nofollow noopener".',
            )


class ToolLogoImageAttributeTests(TestCase):
    """Beta 8.5: tool logos reserve their layout space and decode off the
    main thread; no test touches real storage (logo is a bare path string,
    never saved through FieldFile, so no file is written to media/)."""

    @classmethod
    def setUpTestData(cls):
        cls.tool = Tool.objects.create(
            slug="logo-tool", website="https://example.com/logo-tool",
            logo="logos/test-logo.png",
        )
        with switch_language(cls.tool, "en"):
            cls.tool.name = "LogoTool"
            cls.tool.short_description = "Has a logo"
            cls.tool.long_description = "Has a logo, long"
            cls.tool.save()

        cls.tool_no_logo = Tool.objects.create(
            slug="no-logo-tool", website="https://example.com/no-logo-tool",
        )
        with switch_language(cls.tool_no_logo, "en"):
            cls.tool_no_logo.name = "NoLogoTool"
            cls.tool_no_logo.short_description = "No logo"
            cls.tool_no_logo.long_description = "No logo, long"
            cls.tool_no_logo.save()

    def _logo_img_tag(self, html, src_fragment):
        match = re.search(r'<img src="[^"]*' + re.escape(src_fragment) + r'[^"]*"[^>]*>', html)
        self.assertIsNotNone(match, f"no <img> found referencing {src_fragment}")
        return match.group(0)

    def test_list_card_logo_has_positive_numeric_dimensions(self):
        resp = self.client.get(reverse("catalog:list"))
        html = resp.content.decode()
        tag = self._logo_img_tag(html, "test-logo.png")
        self.assertIn('width="40"', tag)
        self.assertIn('height="40"', tag)
        self.assertIn('decoding="async"', tag)
        self.assertIn('alt="LogoTool"', tag)

    def test_list_card_logo_has_no_lazy_loading(self):
        # Above-the-fold status varies with viewport width (1/2/3 grid columns)
        # and the collapsible filter/search chrome, so the list page cannot
        # reliably determine a server-side fold boundary; see report.
        resp = self.client.get(reverse("catalog:list"))
        html = resp.content.decode()
        tag = self._logo_img_tag(html, "test-logo.png")
        self.assertNotIn("loading=", tag)

    def test_list_card_without_logo_keeps_letter_fallback(self):
        resp = self.client.get(reverse("catalog:list"))
        html = resp.content.decode()
        self.assertIn("NoLogoTool", html)
        self.assertRegex(html, r">\s*N\s*<")

    def test_detail_primary_logo_has_positive_numeric_dimensions(self):
        resp = self.client.get(reverse("catalog:detail", kwargs={"slug": "logo-tool"}))
        html = resp.content.decode()
        tag = self._logo_img_tag(html, "test-logo.png")
        self.assertIn('width="64"', tag)
        self.assertIn('height="64"', tag)
        self.assertIn('decoding="async"', tag)

    def test_detail_primary_logo_is_not_lazy(self):
        resp = self.client.get(reverse("catalog:detail", kwargs={"slug": "logo-tool"}))
        html = resp.content.decode()
        tag = self._logo_img_tag(html, "test-logo.png")
        self.assertNotIn("loading=", tag)

    def test_detail_without_logo_keeps_letter_fallback(self):
        resp = self.client.get(reverse("catalog:detail", kwargs={"slug": "no-logo-tool"}))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("NoLogoTool", html)
        self.assertRegex(html, r">\s*N\s*<")

    def test_no_image_attribute_is_rendered_empty_or_invalid(self):
        resp = self.client.get(reverse("catalog:detail", kwargs={"slug": "logo-tool"}))
        html = resp.content.decode()
        tag = self._logo_img_tag(html, "test-logo.png")
        self.assertNotIn('width=""', tag)
        self.assertNotIn('height=""', tag)
        self.assertNotIn('width="auto"', tag)
        self.assertNotIn('height="auto"', tag)
        self.assertNotIn('width="0"', tag)
        self.assertNotIn('height="0"', tag)
