from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from taggit.models import Tag

from core.models.editorial import EditorialWorkflowMixin
from prompts.models import Prompt


def create_prompt(*, slug, en_title, de_title=None,
                  status=EditorialWorkflowMixin.STATUS_PUBLISHED, published_at=None):
    if published_at is None and status == EditorialWorkflowMixin.STATUS_PUBLISHED:
        published_at = timezone.now()
    p = Prompt.objects.create(slug=slug, status=status, published_at=published_at)
    p.set_current_language("en")
    p.title = en_title
    p.body = "Body EN"
    p.save()
    p.set_current_language("de")
    p.title = de_title or en_title
    p.body = "Body DE"
    p.save()
    return p


class PromptPublicViewsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tag_ai = Tag.objects.create(name="ai")
        cls.p_en_pub = create_prompt(slug="hello-en", en_title="Hello EN")

    def test_list_page_200(self):
        url = reverse("prompts:list")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_detail_canonical_and_og_url_are_present_and_non_empty(self):
        """Companion to the preview's og:url suppression (Beta 11.8): the
        public page must keep a real, non-empty canonical and og:url."""
        # The literal slug from setUpTestData - self.p_en_pub.slug is a
        # translated-field descriptor whose value depends on parler's
        # current-language cache, not a stable identifier to assert on.
        slug = "hello-en"
        resp = self.client.get(reverse("prompts:detail", kwargs={"slug": slug}))
        html = resp.content.decode("utf-8")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('<link rel="canonical"', html)
        self.assertIn('property="og:url"', html)
        self.assertNotIn('property="og:url" content="">', html)
        self.assertIn(slug, html)
