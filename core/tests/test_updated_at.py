"""
Reliability tests for EditorialWorkflowMixin.updated_at (auto_now=True),
run against all four editorial content models.
"""
from django.test import TestCase
from django.utils import timezone
from parler.utils.context import switch_language

from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin
from guides.models import Guide
from prompts.models import Prompt
from usecases.models import UseCase

EXTRA_TRANSLATION_KWARGS = {
    Guide: {},
    Prompt: {"outro": "o"},
    UseCase: {"outro": "o", "persona": "Founder"},
    Comparison: {},
}


def make_instance(model, **kwargs):
    kwargs.setdefault("status", EditorialWorkflowMixin.STATUS_DRAFT)
    obj = model.objects.create(**kwargs)
    obj.create_translation(
        "en", title="Title", intro="i", body="b", slug=f"{model.__name__.lower()}-{obj.pk}",
        **EXTRA_TRANSLATION_KWARGS[model],
    )
    return obj


class UpdatedAtReliabilityMixin:
    model = None

    def test_plain_save_bumps_updated_at(self):
        obj = make_instance(self.model)
        fixed = timezone.now() - timezone.timedelta(days=10)
        self.model.objects.filter(pk=obj.pk).update(updated_at=fixed)
        self.model.objects.get(pk=obj.pk).save()
        self.assertGreater(self.model.objects.get(pk=obj.pk).updated_at, fixed)

    def test_update_fields_without_updated_at_leaves_it_unchanged(self):
        obj = make_instance(self.model)
        fixed = timezone.now() - timezone.timedelta(days=10)
        self.model.objects.filter(pk=obj.pk).update(updated_at=fixed)
        obj2 = self.model.objects.get(pk=obj.pk)
        obj2.review_note = "changed"
        obj2.save(update_fields=["review_note"])
        self.assertEqual(self.model.objects.get(pk=obj.pk).updated_at, fixed)

    def test_update_fields_including_updated_at_bumps_it(self):
        obj = make_instance(self.model)
        fixed = timezone.now() - timezone.timedelta(days=10)
        self.model.objects.filter(pk=obj.pk).update(updated_at=fixed)
        obj2 = self.model.objects.get(pk=obj.pk)
        obj2.review_note = "changed"
        obj2.save(update_fields=["review_note", "updated_at"])
        self.assertGreater(self.model.objects.get(pk=obj.pk).updated_at, fixed)

    def test_reads_never_touch_updated_at(self):
        obj = make_instance(self.model)
        fixed = timezone.now() - timezone.timedelta(days=3)
        self.model.objects.filter(pk=obj.pk).update(updated_at=fixed)
        list(self.model.objects.all())
        self.model.objects.get(pk=obj.pk)
        self.assertEqual(self.model.objects.get(pk=obj.pk).updated_at, fixed)

    def test_published_at_is_not_conflated_with_updated_at(self):
        obj = make_instance(self.model)
        published_fixed = timezone.now() - timezone.timedelta(days=20)
        self.model.objects.filter(pk=obj.pk).update(published_at=published_fixed)
        self.model.objects.get(pk=obj.pk).save()
        refreshed = self.model.objects.get(pk=obj.pk)
        self.assertEqual(refreshed.published_at, published_fixed)
        self.assertNotEqual(refreshed.updated_at, published_fixed)

    def test_publish_transition_bumps_updated_at(self):
        obj = make_instance(self.model, status=EditorialWorkflowMixin.STATUS_APPROVED)
        fixed = timezone.now() - timezone.timedelta(days=5)
        self.model.objects.filter(pk=obj.pk).update(updated_at=fixed)
        obj2 = self.model.objects.get(pk=obj.pk)
        obj2.publish(by=None)
        obj2.save()
        self.assertGreater(self.model.objects.get(pk=obj.pk).updated_at, fixed)

    def test_archive_transition_bumps_updated_at(self):
        obj = make_instance(
            self.model, status=EditorialWorkflowMixin.STATUS_PUBLISHED, published_at=timezone.now()
        )
        fixed = timezone.now() - timezone.timedelta(days=5)
        self.model.objects.filter(pk=obj.pk).update(updated_at=fixed)
        obj2 = self.model.objects.get(pk=obj.pk)
        obj2.archive(by=None)
        obj2.save()
        self.assertGreater(self.model.objects.get(pk=obj.pk).updated_at, fixed)

    def test_translation_edit_followed_by_master_save_bumps_updated_at(self):
        # Mirrors the real admin flow: a TranslatableModelForm always saves
        # the shared master object too, even when only a translated field
        # (title/intro/body/...) changed.
        obj = make_instance(self.model)
        fixed = timezone.now() - timezone.timedelta(days=1)
        self.model.objects.filter(pk=obj.pk).update(updated_at=fixed)
        obj2 = self.model.objects.get(pk=obj.pk)
        with switch_language(obj2, "en"):
            obj2.title = "Updated Title"
            obj2.save_translations()
        obj2.save()
        self.assertGreater(self.model.objects.get(pk=obj.pk).updated_at, fixed)


class GuideUpdatedAtTests(UpdatedAtReliabilityMixin, TestCase):
    model = Guide


class PromptUpdatedAtTests(UpdatedAtReliabilityMixin, TestCase):
    model = Prompt


class UseCaseUpdatedAtTests(UpdatedAtReliabilityMixin, TestCase):
    model = UseCase


class ComparisonUpdatedAtTests(UpdatedAtReliabilityMixin, TestCase):
    model = Comparison
