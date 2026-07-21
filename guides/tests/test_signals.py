from django.test import TestCase
from django.utils import timezone

from core.models.editorial import EditorialWorkflowMixin
from guides.models import Guide, GuideSection
from guides.signals import _move_parent_to_review


def make_guide(status=EditorialWorkflowMixin.STATUS_PUBLISHED, published_at=None):
    if published_at is None and status == EditorialWorkflowMixin.STATUS_PUBLISHED:
        published_at = timezone.now()
    return Guide.objects.create(status=status, published_at=published_at)


def reload(guide):
    """
    Guide has no FSMModelMixin, so refresh_from_db() can't be used on an
    already-loaded instance with a protected status field (a pre-existing,
    out-of-scope gap - see guides/tests/test_starter.py and the coverage
    report); re-fetch instead, as done elsewhere in this app's tests.
    """
    return Guide.objects.get(pk=guide.pk)


def publish(guide):
    """
    FSMField(protected=True) forbids ever re-assigning `.status` directly
    (see django_fsm.FSMFieldDescriptor.__set__), so getting an existing
    guide instance to PUBLISHED - without simply constructing it that way -
    has to go through the real publish() transition, as elsewhere in this
    app's tests (e.g. guides/tests/test_live_revisions.py).
    """
    guide.publish(by=None)
    guide.save()
    return guide


class GuideSectionSignalsTests(TestCase):
    """
    _move_parent_to_review only fires for a *published* guide: creating,
    changing or deleting one of its sections should knock it back into
    review so an editor re-checks the now-stale published content. Guides
    in any other workflow state are already mid-workflow and must be left
    alone.
    """

    def test_creating_section_moves_published_guide_to_review(self):
        guide = make_guide(status=EditorialWorkflowMixin.STATUS_PUBLISHED)

        GuideSection.objects.create(guide=guide, order=1)

        guide = reload(guide)
        self.assertEqual(guide.status, EditorialWorkflowMixin.STATUS_REVIEW)
        self.assertIsNotNone(guide.submitted_for_review_at)

    def test_updating_section_moves_published_guide_to_review(self):
        guide = make_guide(status=EditorialWorkflowMixin.STATUS_APPROVED)
        section = GuideSection.objects.create(guide=guide, order=1)
        publish(guide)

        section.order = 2
        section.save()

        guide = reload(guide)
        self.assertEqual(guide.status, EditorialWorkflowMixin.STATUS_REVIEW)

    def test_deleting_section_moves_published_guide_to_review(self):
        guide = make_guide(status=EditorialWorkflowMixin.STATUS_APPROVED)
        section = GuideSection.objects.create(guide=guide, order=1)
        publish(guide)

        section.delete()

        guide = reload(guide)
        self.assertEqual(guide.status, EditorialWorkflowMixin.STATUS_REVIEW)

    def test_draft_guide_is_left_alone_on_section_create(self):
        guide = make_guide(status=EditorialWorkflowMixin.STATUS_DRAFT)

        GuideSection.objects.create(guide=guide, order=1)

        guide = reload(guide)
        self.assertEqual(guide.status, EditorialWorkflowMixin.STATUS_DRAFT)

    def test_review_guide_is_left_alone_on_section_update(self):
        guide = make_guide(status=EditorialWorkflowMixin.STATUS_REVIEW)
        section = GuideSection.objects.create(guide=guide, order=1)
        self.assertEqual(reload(guide).status, EditorialWorkflowMixin.STATUS_REVIEW)

        section.order = 5
        section.save()

        self.assertEqual(reload(guide).status, EditorialWorkflowMixin.STATUS_REVIEW)

    def test_archived_guide_is_left_alone_on_section_delete(self):
        guide = make_guide(status=EditorialWorkflowMixin.STATUS_ARCHIVED)
        section = GuideSection.objects.create(guide=guide, order=1)

        section.delete()

        guide = reload(guide)
        self.assertEqual(guide.status, EditorialWorkflowMixin.STATUS_ARCHIVED)

    def test_saving_guide_itself_does_not_trigger_the_section_receiver(self):
        guide = make_guide(status=EditorialWorkflowMixin.STATUS_PUBLISHED)

        guide.review_note = "unrelated edit"
        guide.save()

        guide = reload(guide)
        self.assertEqual(guide.status, EditorialWorkflowMixin.STATUS_PUBLISHED)

    def test_cascade_deleting_the_guide_does_not_error(self):
        guide = make_guide(status=EditorialWorkflowMixin.STATUS_PUBLISHED)
        GuideSection.objects.create(guide=guide, order=1)

        guide.delete()

        self.assertFalse(GuideSection.objects.exists())


class MoveParentToReviewHelperTests(TestCase):
    """
    Direct unit tests for the private helper's guard clause covering the
    "no related guide" case, which is not reachable through normal ORM
    operations since GuideSection.guide is a required (non-nullable) FK -
    it only matters if the related row is ever missing at signal time.
    """

    def test_no_guide_is_a_no_op(self):
        self.assertIsNone(_move_parent_to_review(None, "note"))

    def test_non_published_guide_is_a_no_op(self):
        guide = make_guide(status=EditorialWorkflowMixin.STATUS_DRAFT)
        _move_parent_to_review(guide, "note")
        guide = reload(guide)
        self.assertEqual(guide.status, EditorialWorkflowMixin.STATUS_DRAFT)
