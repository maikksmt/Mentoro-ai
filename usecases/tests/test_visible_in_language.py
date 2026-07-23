"""
Beta 8.9a: UseCase.objects.visible_in_language() - the new strict,
explicit-language public queryset (mirrors Prompt.objects.visible_in_language()
from Beta 8.8), reused by UseCaseListView, UseCaseDetailView,
get_latest_items() and related_usecases().
"""
from django.test import TestCase
from django.utils import timezone

from core.models.editorial import EditorialWorkflowMixin
from usecases.models import UseCase


def make_usecase(*, slug, status=EditorialWorkflowMixin.STATUS_PUBLISHED,
                  published_at=None, languages=("en", "de"), **extra):
    if published_at is None and status == EditorialWorkflowMixin.STATUS_PUBLISHED:
        published_at = timezone.now()
    u = UseCase.objects.create(status=status, published_at=published_at, **extra)
    for lang in languages:
        u.create_translation(lang, title=f"Title {slug} {lang}", intro="i", body="b", outro="o",
                              slug=f"{slug}-{lang}", persona="")
    return u


class UseCaseVisibleInLanguageTests(TestCase):
    def test_published_usecase_with_active_translation_returned_en(self):
        u = make_usecase(slug="active-en", languages=("en",))
        self.assertIn(u, UseCase.objects.visible_in_language("en"))

    def test_published_usecase_with_active_translation_returned_de(self):
        u = make_usecase(slug="active-de", languages=("de",))
        self.assertIn(u, UseCase.objects.visible_in_language("de"))

    def test_usecase_without_active_translation_excluded_en(self):
        u = make_usecase(slug="no-active-en", languages=("de",))
        self.assertNotIn(u, UseCase.objects.visible_in_language("en"))

    def test_usecase_without_active_translation_excluded_de(self):
        u = make_usecase(slug="no-active-de", languages=("en",))
        self.assertNotIn(u, UseCase.objects.visible_in_language("de"))

    def test_en_only_absent_from_de_queryset(self):
        u = make_usecase(slug="en-only-strict", languages=("en",))
        self.assertNotIn(u, UseCase.objects.visible_in_language("de"))

    def test_de_only_absent_from_en_queryset(self):
        u = make_usecase(slug="de-only-strict", languages=("de",))
        self.assertNotIn(u, UseCase.objects.visible_in_language("en"))

    def test_bilingual_usecase_appears_in_both_languages(self):
        u = make_usecase(slug="bilingual-strict", languages=("en", "de"))
        self.assertIn(u, UseCase.objects.visible_in_language("en"))
        self.assertIn(u, UseCase.objects.visible_in_language("de"))

    def test_draft_never_appears(self):
        u = make_usecase(slug="draft-strict", status=EditorialWorkflowMixin.STATUS_DRAFT,
                          published_at=None, languages=("en", "de"))
        self.assertNotIn(u, UseCase.objects.visible_in_language("en"))
        self.assertNotIn(u, UseCase.objects.visible_in_language("de"))

    def test_review_without_a_live_revision_stays_excluded(self):
        # Beta 11.7 widened visible_in_language() to visible_on_site(), but
        # that branch still requires last_published_revision_id - a use case
        # sent to review before it was ever published must stay invisible.
        u = make_usecase(slug="review-strict", status=EditorialWorkflowMixin.STATUS_REVIEW,
                          published_at=None, languages=("en",))
        self.assertNotIn(u, UseCase.objects.visible_in_language("en"))

    def test_review_with_a_live_revision_is_included(self):
        # The Beta 11.7 contract: a previously published use case keeps its
        # public presence through a new review round.
        u = make_usecase(slug="review-with-live", status=EditorialWorkflowMixin.STATUS_REVIEW,
                          published_at=None, languages=("en",), last_published_revision_id=1)
        self.assertIn(u, UseCase.objects.visible_in_language("en"))

    def test_no_duplicates_from_translation_join(self):
        for i in range(5):
            make_usecase(slug=f"nodup-strict-{i}", languages=("en", "de"))
        qs = UseCase.objects.visible_in_language("en")
        pks = list(qs.values_list("pk", flat=True))
        self.assertEqual(len(pks), len(set(pks)))
