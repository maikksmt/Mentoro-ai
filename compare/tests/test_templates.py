from django.test import TestCase
from django.urls import reverse
from django.utils import timezone, translation

from compare.models import Comparison


class ComparisonDetailReadingAxisTests(TestCase):
    """
    Beta 9.5: comparison_detail.html is the one detail page that deliberately
    keeps a full-width shell (its tool-comparison grid needs the extra room),
    so the breadcrumbs, header, intro and body must all be explicitly opted
    into the centered reading axis instead of inheriting it from a
    `.prose.reading-column` article wrapper like the other detail pages -
    while the comparison grid and Related Comparisons stay at full shell
    width. See guides.tests.test_templates for the same underlying fix.
    """

    @classmethod
    def setUpTestData(cls):
        cls.comparison = Comparison.objects.create(
            status="published", published_at=timezone.now()
        )
        cls.comparison.create_translation(
            "en",
            title="Axis Comparison",
            intro="<p>Intro</p>",
            body="<p>Body</p>",
            slug="axis-comparison-en",
        )

    def _get_detail_html(self) -> str:
        with translation.override("en"):
            url = reverse("compare:detail", kwargs={"slug": "axis-comparison-en"})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_breadcrumbs_and_header_share_the_reading_axis(self):
        html = self._get_detail_html()
        breadcrumbs_wrapper_start = html.index('class="detail-reading-header"')
        breadcrumbs_start = html.index('class="text-sm breadcrumbs"')
        header_start = html.index('class="mb-6 detail-reading-header"')
        intro_start = html.index("page-lead")
        body_start = html.index('class="prose reading-column my-6"')

        self.assertLess(breadcrumbs_wrapper_start, breadcrumbs_start)
        self.assertLess(breadcrumbs_start, header_start)
        self.assertLess(header_start, intro_start)
        self.assertLess(intro_start, body_start)

    def test_intro_and_body_use_the_narrow_reading_column(self):
        html = self._get_detail_html()
        self.assertIn('class="prose reading-column page-lead"', html)
        self.assertIn('class="prose reading-column my-6"', html)
