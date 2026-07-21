"""
Coverage-Schritt 3: GuideItem (guides/models.py) - get_title()/get_teaser()/
get_url()/clean() had no test coverage before this slice (no test file in
the repository references GuideItem at all).
"""
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import translation

from core.models.editorial import EditorialWorkflowMixin
from guides.models import Guide, GuideItem, GuideSection


def make_section():
    guide = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_DRAFT)
    return GuideSection.objects.create(guide=guide, order=1)


class GuideItemTitleTeaserTests(TestCase):
    def test_get_title_uses_its_own_translation_when_present(self):
        item = GuideItem.objects.create(section=make_section(), url="https://example.com")
        item.create_translation("en", title="Own Title", teaser="Own Teaser")
        with translation.override("en"):
            self.assertEqual(item.get_title(), "Own Title")
            self.assertEqual(item.get_teaser(), "Own Teaser")

    def test_get_title_falls_back_to_content_object_title_when_own_is_blank(self):
        guide = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_DRAFT)
        guide.create_translation("en", title="Linked Guide Title", intro="i", body="b", slug="linked-guide-item")
        item = GuideItem.objects.create(
            section=make_section(),
            content_type=ContentType.objects.get_for_model(Guide),
            object_id=guide.pk,
        )
        with translation.override("en"):
            self.assertEqual(item.get_title(), "Linked Guide Title")

    def test_get_title_falls_back_to_item_when_neither_own_nor_object_has_a_title(self):
        item = GuideItem.objects.create(section=make_section(), url="https://example.com")
        with translation.override("en"):
            self.assertEqual(item.get_title(), "Item")

    def test_get_teaser_falls_back_to_content_object_body_when_own_is_blank(self):
        guide = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_DRAFT)
        guide.create_translation("en", title="T", intro="i", body="Linked Body", slug="linked-guide-teaser")
        item = GuideItem.objects.create(
            section=make_section(),
            content_type=ContentType.objects.get_for_model(Guide),
            object_id=guide.pk,
        )
        with translation.override("en"):
            self.assertEqual(item.get_teaser(), "Linked Body")

    def test_get_teaser_empty_when_neither_own_nor_object_has_one(self):
        item = GuideItem.objects.create(section=make_section(), url="https://example.com")
        with translation.override("en"):
            self.assertEqual(item.get_teaser(), "")


class GuideItemUrlTests(TestCase):
    def test_get_url_uses_own_url_field_when_set(self):
        item = GuideItem.objects.create(section=make_section(), url="https://example.com/own/")
        self.assertEqual(item.get_url(), "https://example.com/own/")

    def test_get_url_falls_back_to_content_objects_absolute_url(self):
        guide = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_DRAFT)
        guide.create_translation("en", title="T", intro="i", body="b", slug="linked-guide-url")
        item = GuideItem.objects.create(
            section=make_section(),
            content_type=ContentType.objects.get_for_model(Guide),
            object_id=guide.pk,
        )
        with translation.override("en"):
            self.assertEqual(item.get_url(), guide.get_absolute_url())

    def test_get_url_returns_hash_when_neither_url_nor_content_object(self):
        item = GuideItem.objects.create(section=make_section())
        self.assertEqual(item.get_url(), "#")


class GuideItemValidationTests(TestCase):
    def test_clean_passes_with_a_plain_url(self):
        item = GuideItem(section=make_section(), url="https://example.com")
        item.clean()  # must not raise

    def test_clean_passes_with_a_content_object(self):
        guide = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_DRAFT)
        item = GuideItem(
            section=make_section(),
            content_type=ContentType.objects.get_for_model(Guide),
            object_id=guide.pk,
        )
        item.clean()  # must not raise

    def test_clean_raises_when_neither_url_nor_content_object_given(self):
        item = GuideItem(section=make_section())
        with self.assertRaises(ValidationError):
            item.clean()

    def test_clean_raises_when_only_content_type_without_object_id(self):
        item = GuideItem(section=make_section(), content_type=ContentType.objects.get_for_model(Guide))
        with self.assertRaises(ValidationError):
            item.clean()


class GuideItemStrTests(TestCase):
    def test_str_includes_kind_and_pk(self):
        item = GuideItem.objects.create(section=make_section(), kind="tool", url="https://example.com")
        self.assertIn("tool", str(item))
        self.assertIn(str(item.pk), str(item))
