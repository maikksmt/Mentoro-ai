from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from core.models.editorial import EditorialWorkflowMixin
from guides.models import Guide


def make_guide(*, slug, title="Guide", status=EditorialWorkflowMixin.STATUS_PUBLISHED,
                published_at=None, is_starter=False):
    if published_at is None and status == EditorialWorkflowMixin.STATUS_PUBLISHED:
        published_at = timezone.now()
    g = Guide.objects.create(status=status, published_at=published_at, is_starter=is_starter)
    g.create_translation("en", slug=f"{slug}-en", title=title, intro="i", body="b")
    return g


class IsStarterFieldTests(TestCase):
    def test_default_is_false(self):
        g = make_guide(slug="plain", status=EditorialWorkflowMixin.STATUS_DRAFT, published_at=None)
        self.assertFalse(g.is_starter)

    def test_a_guide_can_be_marked_as_starter(self):
        g = make_guide(slug="starter", is_starter=True)
        # Guide has no FSMModelMixin, so refresh_from_db() can't be used on
        # an already-loaded instance with a protected status field (a
        # pre-existing, out-of-scope gap - see report); re-fetch instead.
        self.assertTrue(Guide.objects.get(pk=g.pk).is_starter)


class StarterDraftConstellationTests(TestCase):
    """Multiple drafts may be flagged is_starter=True at once; the
    constraint only ever blocks a second *published* starter."""

    def test_multiple_draft_starters_are_allowed(self):
        make_guide(slug="draft-a", status=EditorialWorkflowMixin.STATUS_DRAFT, published_at=None, is_starter=True)
        make_guide(slug="draft-b", status=EditorialWorkflowMixin.STATUS_DRAFT, published_at=None, is_starter=True)
        self.assertEqual(
            Guide.objects.filter(is_starter=True, status=EditorialWorkflowMixin.STATUS_DRAFT).count(), 2
        )

    def test_draft_starter_alongside_published_starter_is_allowed(self):
        make_guide(slug="published-starter", is_starter=True)
        make_guide(slug="candidate-draft", status=EditorialWorkflowMixin.STATUS_DRAFT, published_at=None, is_starter=True)
        self.assertEqual(Guide.objects.filter(is_starter=True).count(), 2)


class CompetingPublishedStarterTests(TestCase):
    def setUp(self):
        self.first = make_guide(slug="first-starter", is_starter=True)

    def test_full_clean_rejects_a_second_published_starter(self):
        second = make_guide(slug="second-starter", is_starter=False)
        second.is_starter = True
        # Mirrors GuideAdmin: "status" is admin-readonly, so ModelForm._post_clean()
        # calls full_clean(exclude=[...,"status"]); status is also FSM-protected
        # and can't be re-set via clean_fields()'s setattr like other fields.
        with self.assertRaises(ValidationError):
            second.full_clean(exclude=["status"])

    def test_database_constraint_rejects_a_second_published_starter(self):
        second = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_PUBLISHED, published_at=timezone.now())
        second.create_translation("en", slug="second-starter-en", title="Second", intro="i", body="b")
        second.is_starter = True
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Guide.objects.filter(pk=second.pk).update(is_starter=True)

    def test_unpublishing_the_starter_frees_the_position(self):
        self.first.archive(by=None)
        self.first.save()

        second = make_guide(slug="second-starter", is_starter=False)
        second.is_starter = True
        second.full_clean(exclude=["status"])  # must not raise: no other *published* starter remains
        second.save()
        self.assertTrue(Guide.objects.get(pk=second.pk).is_starter)
