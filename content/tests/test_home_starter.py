from django.test import TestCase
from django.utils import timezone

from content.views.home import resolve_starter_guide_url
from core.models.editorial import EditorialWorkflowMixin
from guides.models import Guide


def make_guide(*, slug, status=EditorialWorkflowMixin.STATUS_PUBLISHED,
                published_at=None, is_starter=False, languages=("en",)):
    if published_at is None and status == EditorialWorkflowMixin.STATUS_PUBLISHED:
        published_at = timezone.now()
    g = Guide.objects.create(status=status, published_at=published_at, is_starter=is_starter)
    for lang in languages:
        g.create_translation(lang, slug=f"{slug}-{lang}", title=f"Guide {slug}", intro="i", body="b")
    return g


class ResolveStarterGuideUrlTests(TestCase):
    def test_published_translated_starter_is_used(self):
        starter = make_guide(slug="anything-goes", is_starter=True, languages=("en", "de"))
        url = resolve_starter_guide_url("en")
        self.assertEqual(url, starter.get_absolute_url(language="en"))

    def test_works_regardless_of_slug(self):
        # No "start-guide-*" naming convention involved at all: is_starter
        # alone determines the CTA target.
        starter = make_guide(slug="totally-unrelated-name", is_starter=True)
        self.assertEqual(resolve_starter_guide_url("en"), starter.get_absolute_url(language="en"))

    def test_legacy_start_guide_slug_alone_is_not_enough(self):
        make_guide(slug="start-guide", is_starter=False)
        self.assertIsNone(resolve_starter_guide_url("en"))

    def test_unpublished_starter_is_not_linked(self):
        make_guide(
            slug="draft-starter", is_starter=True,
            status=EditorialWorkflowMixin.STATUS_DRAFT, published_at=None,
        )
        self.assertIsNone(resolve_starter_guide_url("en"))

    def test_missing_starter_returns_none(self):
        make_guide(slug="regular", is_starter=False)
        self.assertIsNone(resolve_starter_guide_url("en"))

    def test_starter_without_requested_translation_is_not_shown_as_wrong_language(self):
        # PARLER_LANGUAGES has an 'en' fallback with hide_untranslated=False,
        # so an EN-only guide could otherwise leak in as if it were the DE CTA.
        make_guide(slug="en-only-starter", is_starter=True, languages=("en",))
        self.assertIsNone(resolve_starter_guide_url("de"))

    def test_starter_with_requested_translation_is_shown(self):
        starter = make_guide(slug="bilingual-starter", is_starter=True, languages=("en", "de"))
        self.assertEqual(resolve_starter_guide_url("de"), starter.get_absolute_url(language="de"))


class HomeStarterCtaTests(TestCase):
    def test_cta_links_to_published_starter(self):
        starter = make_guide(slug="home-cta-starter", is_starter=True)
        resp = self.client.get("/en/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn(starter.get_absolute_url(language="en"), html)

    def test_cta_omits_link_when_no_starter_exists(self):
        resp = self.client.get("/en/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        # The guarded template must not render an empty href="" anchor.
        self.assertNotIn('href=""', html)
