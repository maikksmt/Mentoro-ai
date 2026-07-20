"""
Beta 8.9a: UseCaseDetailView must be status- and language-safe.

Confirmed empirically (before the fix): UseCase.objects.all() had neither a
status filter (a draft use case was publicly resolvable by direct slug URL)
nor a language filter (an EN-only use case rendered its English title/body
under a /de/... URL with HTTP 200, instead of 404ing) - see the Beta 8.9a
report for the exact shell reproduction. Both are fixed via
UseCase.objects.visible_in_language(lang), mirroring PromptDetailView's
Beta 8.8 strict resolution.
"""
from django.test import TestCase
from django.utils import timezone

from core.models.editorial import EditorialWorkflowMixin
from usecases.models import UseCase


def make_usecase(*, slug, status=EditorialWorkflowMixin.STATUS_PUBLISHED,
                  published_at=None, languages=("en", "de"), persona="", **extra):
    if published_at is None and status == EditorialWorkflowMixin.STATUS_PUBLISHED:
        published_at = timezone.now()
    u = UseCase.objects.create(status=status, published_at=published_at, **extra)
    for lang in languages:
        u.create_translation(lang, title=f"Title {slug} {lang}", intro="i", body="b", outro="o",
                              slug=f"{slug}-{lang}", persona=persona)
    return u


class DetailPageWithPersonaTests(TestCase):
    def test_en_detail_with_persona_returns_200(self):
        make_usecase(slug="detail-persona-en", languages=("en",), persona="Founder")
        resp = self.client.get("/en/usecases/detail-persona-en-en/")
        self.assertEqual(resp.status_code, 200)

    def test_de_detail_with_persona_returns_200(self):
        make_usecase(slug="detail-persona-de", languages=("de",), persona="Gründer")
        resp = self.client.get("/de/usecases/detail-persona-de-de/")
        self.assertEqual(resp.status_code, 200)

    def test_related_area_does_not_cause_500(self):
        make_usecase(slug="detail-persona-no500", languages=("en",), persona="Marketer")
        resp = self.client.get("/en/usecases/detail-persona-no500-en/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("similar", resp.context)


class DetailPageLanguageIsolationTests(TestCase):
    def test_en_only_usecase_absent_under_de_prefix(self):
        make_usecase(slug="en-only-detail-strict", languages=("en",))
        resp = self.client.get("/de/usecases/en-only-detail-strict-en/")
        self.assertEqual(resp.status_code, 404)

    def test_de_only_usecase_absent_under_en_prefix(self):
        make_usecase(slug="de-only-detail-strict", languages=("de",))
        resp = self.client.get("/en/usecases/de-only-detail-strict-de/")
        self.assertEqual(resp.status_code, 404)

    def test_bilingual_usecase_reachable_in_both_languages(self):
        make_usecase(slug="bilingual-detail-strict", languages=("en", "de"))
        self.assertEqual(self.client.get("/en/usecases/bilingual-detail-strict-en/").status_code, 200)
        self.assertEqual(self.client.get("/de/usecases/bilingual-detail-strict-de/").status_code, 200)


class DraftNotPubliclyReachableTests(TestCase):
    def test_draft_usecase_returns_404(self):
        make_usecase(slug="draft-detail-strict", status=EditorialWorkflowMixin.STATUS_DRAFT,
                     published_at=None, languages=("en",))
        resp = self.client.get("/en/usecases/draft-detail-strict-en/")
        self.assertEqual(resp.status_code, 404)


class RelatedContentOnDetailPageTests(TestCase):
    def test_current_usecase_not_shown_in_its_own_related_list(self):
        current = make_usecase(slug="self-not-related", languages=("en",), persona="Founder")
        make_usecase(slug="self-not-related-filler", languages=("en",), persona="Founder")
        resp = self.client.get("/en/usecases/self-not-related-en/")
        self.assertEqual(resp.status_code, 200)
        similar_urls = [item["url"] for item in resp.context["similar"]]
        self.assertNotIn(current.get_absolute_url(language="en"), similar_urls)

    def test_related_links_do_not_404(self):
        make_usecase(slug="related-links-check", languages=("en",), persona="Founder")
        for i in range(3):
            make_usecase(slug=f"related-links-filler-{i}", languages=("en",), persona="Founder")
        resp = self.client.get("/en/usecases/related-links-check-en/")
        self.assertEqual(resp.status_code, 200)
        for item in resp.context["similar"]:
            check = self.client.get(item["url"])
            self.assertEqual(check.status_code, 200)
