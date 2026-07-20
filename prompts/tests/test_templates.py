import re

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone, translation

from prompts.models import Prompt

COPY_MARKERS = (
    "data-copy",
    "copy-to-clipboard",
    'aria-label="Copy"',
    "btn-copy",
    "hx-copy",
)


class PromptTemplateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # Explicit language context: Prompt.objects.create() with translated
        # kwargs creates the translation in whatever Django's *ambient*
        # active language happens to be at call time. setUpTestData() runs
        # outside any per-test override(), so without this it silently
        # depends on test execution order (e.g. an earlier test's real
        # /de/... request leaving the ambient language activated to 'de').
        with translation.override("en"):
            cls.p_en = Prompt.objects.create(
                title="Copy Me",
                slug="copy-me",
                body="Use this",
                status="published",
                published_at=timezone.now(),
            )

    def _has_any_copy_marker(self, html: str) -> bool:
        return any(m in html for m in COPY_MARKERS)

    def test_list_template_has_usage_or_copy_ui_if_present(self):
        with translation.override("en"):
            url = reverse("prompts:list")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        if self._has_any_copy_marker(html):
            self.assertTrue(True)
        else:
            self.assertIn("Prompts", html)

    def test_detail_template_has_usage_or_copy_ui_if_present(self):
        with translation.override("en"):
            url = reverse("prompts:detail", kwargs={"slug": self.p_en.slug})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()

        if self._has_any_copy_marker(html):
            self.assertTrue(True)
        else:
            self.assertIn(self.p_en.title, html)
            self.assertIn("Use this", html)


class PromptBlockStructureTests(TestCase):
    """Covers Beta 8.1: full prompt visibility, no internal scroll area,
    non-overlapping copy button, and i18n-driven copy feedback."""

    @classmethod
    def setUpTestData(cls):
        cls.long_marker = "END-OF-PROMPT-MARKER-1234567890"
        cls.long_body = "<p>" + ("This is a very long prompt line. " * 200) + cls.long_marker + "</p>"
        # See PromptTemplateTests.setUpTestData for why this must be explicit.
        with translation.override("en"):
            cls.prompt = Prompt.objects.create(
                title="Long Prompt",
                slug="long-prompt",
                body=cls.long_body,
                status="published",
                published_at=timezone.now(),
            )

    def _get_detail_html(self) -> str:
        with translation.override("en"):
            url = reverse("prompts:detail", kwargs={"slug": self.prompt.slug})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_full_prompt_body_is_rendered_without_truncation(self):
        html = self._get_detail_html()
        self.assertIn(self.long_marker, html)
        self.assertNotIn("truncat", html.lower())

    def test_no_fixed_max_height_or_internal_scroll_area(self):
        html = self._get_detail_html()
        self.assertNotIn("max-h-[70vh]", html)
        self.assertNotIn("overflow-auto", html)
        self.assertNotIn("overflow-scroll", html)

    def test_copy_button_is_not_absolutely_positioned_over_content(self):
        html = self._get_detail_html()
        match = re.search(r'<button[^>]*class="([^"]*copy-btn[^"]*)"', html)
        self.assertIsNotNone(match, "copy button not found")
        button_classes = match.group(1)
        self.assertNotIn("absolute", button_classes)

    def test_header_contains_prompt_label(self):
        html = self._get_detail_html()
        self.assertIn(">Prompt<", html)

    def test_prompt_label_is_not_rendered_as_a_heading(self):
        # Beta 9.4: the "Prompt" label above the copy block is a visual
        # caption for that widget, not a document heading - it used to be
        # an <h2>, which put a second, unrelated "heading" right before
        # this page's real content (e.g. "Suitable tools") in the
        # document outline. It must render as a non-heading element while
        # keeping the same visible text and styling.
        html = self._get_detail_html()
        self.assertNotRegex(html, r"<h[1-6][^>]*>\s*Prompt\s*<")

    def test_copy_button_exposes_i18n_status_data_attributes(self):
        html = self._get_detail_html()
        self.assertIn("data-copy-default-label=", html)
        self.assertIn("data-copy-success=", html)
        self.assertIn("data-copy-error=", html)

    def test_visible_and_copy_source_content_are_the_same_node(self):
        html = self._get_detail_html()
        self.assertEqual(html.count('data-copy-source>'), 1)
        self.assertIn(self.long_marker, html)

    def test_breadcrumbs_share_the_reading_axis_with_the_article(self):
        """Beta 9.5: see guides.tests.test_templates for the same fix/rationale."""
        html = self._get_detail_html()
        header_start = html.index('class="detail-reading-header"')
        breadcrumbs_start = html.index('class="text-sm breadcrumbs"')
        article_start = html.index('class="prose reading-column"')
        self.assertLess(header_start, breadcrumbs_start)
        self.assertLess(breadcrumbs_start, article_start)
