from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models.editorial import EditorialWorkflowMixin
from guides.models import Guide

User = get_user_model()


def make_guide(*, slug, status=EditorialWorkflowMixin.STATUS_PUBLISHED,
                published_at=None, is_starter=False, title="Guide"):
    if published_at is None and status == EditorialWorkflowMixin.STATUS_PUBLISHED:
        published_at = timezone.now()
    g = Guide.objects.create(status=status, published_at=published_at, is_starter=is_starter)
    g.create_translation("en", slug=f"{slug}-en", title=title, intro="i", body="b")
    return g


class GuideAdminChangeFormTestCase(TestCase):
    """Shared helpers for posting to GuideAdmin's parler change form."""

    def setUp(self):
        self.admin_user = User.objects.create_superuser(
            username="admin", email="admin@example.com", password="pw"
        )
        self.client.force_login(self.admin_user)

    def _change_url(self, guide):
        return reverse("admin:guides_guide_change", args=[guide.pk])

    def _base_payload(self, guide, **overrides):
        data = {
            "author": "",
            "review_note": "",
            "published_at_0": "",
            "published_at_1": "",
            "categories": [],
            "tools": [],
            "slug": guide.safe_translation_getter("slug", language_code="en"),
            "title": guide.safe_translation_getter("title", language_code="en"),
            "intro": guide.safe_translation_getter("intro", language_code="en"),
            "body": guide.safe_translation_getter("body", language_code="en"),
            "sections-TOTAL_FORMS": "0",
            "sections-INITIAL_FORMS": "0",
            "sections-MIN_NUM_FORMS": "0",
            "sections-MAX_NUM_FORMS": "1000",
            "_save": "Save",
        }
        data.update(overrides)
        return data


class IsStarterAdminFormTests(GuideAdminChangeFormTestCase):
    def test_is_starter_field_visible_in_change_form(self):
        guide = make_guide(slug="visible-field", status=EditorialWorkflowMixin.STATUS_DRAFT, published_at=None)
        resp = self.client.get(self._change_url(guide))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("is_starter", resp.context["adminform"].form.fields)

    def test_is_starter_can_be_set_via_the_admin_form(self):
        guide = make_guide(slug="editable-field", status=EditorialWorkflowMixin.STATUS_DRAFT, published_at=None)
        payload = self._base_payload(guide, is_starter="on")
        resp = self.client.post(self._change_url(guide), data=payload)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Guide.objects.get(pk=guide.pk).is_starter)

    def test_translation_fields_still_present_and_functional(self):
        guide = make_guide(slug="translation-flow", status=EditorialWorkflowMixin.STATUS_DRAFT, published_at=None)
        payload = self._base_payload(guide, title="Updated via admin", intro="new intro")
        resp = self.client.post(self._change_url(guide), data=payload)
        self.assertEqual(resp.status_code, 302)
        refreshed = Guide.objects.get(pk=guide.pk)
        self.assertEqual(refreshed.safe_translation_getter("title", language_code="en"), "Updated via admin")
        self.assertEqual(refreshed.safe_translation_getter("intro", language_code="en"), "new intro")


class IsStarterAdminConflictTests(GuideAdminChangeFormTestCase):
    def setUp(self):
        super().setUp()
        self.first = make_guide(slug="first-starter", is_starter=True)

    def test_conflict_surfaces_as_a_form_error_not_a_500(self):
        second = make_guide(slug="second-starter", is_starter=False)
        payload = self._base_payload(second, is_starter="on")
        resp = self.client.post(self._change_url(second), data=payload)
        # Rejected with a normal re-rendered form (200), never an uncaught
        # IntegrityError/500 — Guide.clean() catches it via full_clean().
        self.assertEqual(resp.status_code, 200)
        self.assertIn("is_starter", resp.context["adminform"].form.errors)
        self.assertFalse(Guide.objects.get(pk=second.pk).is_starter)

    def test_action_publish_skips_conflicting_starter_without_crashing(self):
        second = make_guide(
            slug="approved-starter", is_starter=True,
            status=EditorialWorkflowMixin.STATUS_APPROVED, published_at=None,
        )
        url = reverse("admin:guides_guide_changelist")
        resp = self.client.post(url, data={
            "action": "action_publish",
            "_selected_action": [str(second.pk)],
        }, follow=True)
        self.assertEqual(resp.status_code, 200)
        refreshed = Guide.objects.get(pk=second.pk)
        self.assertEqual(refreshed.status, EditorialWorkflowMixin.STATUS_APPROVED)
        self.assertTrue(
            Guide.objects.filter(pk=self.first.pk, is_starter=True, status="published").exists()
        )
