from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone, translation

from core.models.editorial import EditorialWorkflowMixin
from guides.models import Guide

User = get_user_model()


def make_pub():
    author = User.objects.create_user(
        username="john-doe",
        email="john@example.com",
        password="testpass123",
        first_name="John",
        last_name="Doe",
    )

    g = Guide.objects.create(
        status=EditorialWorkflowMixin.STATUS_PUBLISHED,
        published_at=timezone.now(),
        author=author,
    )
    g.create_translation("en", slug="hello-en", title="Hello EN", intro="i", body="b")
    g.create_translation("de", slug="hello-de", title="Hallo DE", intro="i", body="b")
    return g


class GuideTemplateContextTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.pub = make_pub()

    def test_list_template_context_and_meta(self):
        with translation.override("en"):
            url = reverse("guides:list")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertIn("Guides", html)

    def test_detail_template_context_and_meta(self):
        # self.pub.slug (the raw Parler property) reflects whatever language
        # was last active on this in-memory instance (from make_pub()'s
        # final create_translation("de", ...) call), not the "en" override
        # below - translation.override() only affects Django's ambient
        # language, not Parler's per-object current-language state. Use the
        # explicit-language getter instead so this test targets the actual
        # EN slug under the EN prefix.
        en_slug = self.pub.safe_translation_getter("slug", language_code="en")
        with translation.override("en"):
            url = reverse("guides:detail", kwargs={"slug": en_slug})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertIn("Guides", html)
        self.assertIn(self.pub.safe_translation_getter("title", language_code="en"), html)
        self.assertTrue(len(html) > 0)

    def test_breadcrumbs_share_the_reading_axis_with_the_article(self):
        """
        Beta 9.5: the breadcrumb trail used to render at full shell width
        while the article body was narrowed to a centered reading column,
        so breadcrumbs/title started well left of the article's visual
        center. Wrapping the breadcrumbs in .detail-reading-header (the
        same centered, ch-capped width as .reading-column) keeps both on
        one axis.
        """
        en_slug = self.pub.safe_translation_getter("slug", language_code="en")
        with translation.override("en"):
            url = reverse("guides:detail", kwargs={"slug": en_slug})
        resp = self.client.get(url)
        html = resp.content.decode("utf-8")
        header_start = html.index('class="detail-reading-header"')
        breadcrumbs_start = html.index('class="text-sm breadcrumbs"')
        article_start = html.index('class="prose reading-column"')
        self.assertLess(header_start, breadcrumbs_start)
        self.assertLess(breadcrumbs_start, article_start)
