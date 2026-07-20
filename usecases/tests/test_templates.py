from django.test import TestCase
from django.urls import reverse
from django.utils import timezone, translation
from parler.utils.context import switch_language

from core.models.editorial import EditorialWorkflowMixin
from usecases.models import UseCase


class TestTemplates(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.uc = UseCase.objects.create(
            status=EditorialWorkflowMixin.STATUS_PUBLISHED,
            published_at=timezone.now(),
        )
        # DE
        with switch_language(cls.uc, "de"):
            cls.uc.title = "Workflow Demo"
            cls.uc.slug = "workflow-demo-de",
            cls.uc.intro = "Intro DE"
            cls.uc.save()
        # EN
        with switch_language(cls.uc, "en"):
            cls.uc.title = "Workflow Demo"
            cls.uc.slug = "workflow-demo-en",
            cls.uc.intro = "Intro EN"
            cls.uc.save()


class UsecaseDetailReadingAxisTests(TestCase):
    """Beta 9.5: see guides.tests.test_templates for the same fix/rationale."""

    @classmethod
    def setUpTestData(cls):
        cls.uc = UseCase.published.create(
            slug="axis-usecase",
            status=EditorialWorkflowMixin.STATUS_PUBLISHED,
            published_at=timezone.now(),
        )
        with switch_language(cls.uc, "en"):
            cls.uc.title = "Axis Usecase"
            cls.uc.intro = "Intro"
            cls.uc.save()

    def test_breadcrumbs_share_the_reading_axis_with_the_article(self):
        with translation.override("en"):
            url = reverse("usecases:detail", kwargs={"slug": "axis-usecase"})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        header_start = html.index('class="detail-reading-header"')
        breadcrumbs_start = html.index('class="text-sm breadcrumbs"')
        article_start = html.index('class="prose reading-column"')
        self.assertLess(header_start, breadcrumbs_start)
        self.assertLess(breadcrumbs_start, article_start)
