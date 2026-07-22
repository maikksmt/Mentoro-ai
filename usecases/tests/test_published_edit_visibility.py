"""
Beta 11.7: the regression guard for the defect this module used to merely
reproduce.

History: Beta 11.1 confirmed - through the real admin POST an editor
actually uses, not model-level transitions - that editing a published
UseCase took its public detail page offline. The admin's auto-review guard
(EditorialWorkflowAdminMixin.save_model() ->_must_auto_review() ->
_auto_transition_to_review()) moves any changed, previously-PUBLISHED object
to STATUS_REVIEW by design, and UseCaseQuerySet.visible_in_language() then
used the strict .published()-only status rule, so the whole page 404ed even
though the published live_i18n snapshot was still intact. Guide and Prompt
were unaffected because they already used visible_on_site().

Beta 11.1 deliberately did not widen UseCase to visible_on_site(), because
one field would have leaked: UseCase.persona was absent from
LIVE_SNAPSHOT_FIELDS while templates/usecases/list.html rendered it straight
off the current translation (`obj.persona`). Beta 11.7 closes that first -
persona is snapshotted and the card reads display_persona (see
usecases/tests/test_live_visibility_persona_and_cache.py) - and only then
widens the status rule.

This module now asserts the fixed contract: the page stays up, and it keeps
showing the published values.
"""
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from core.models.editorial import EditorialWorkflowMixin
from usecases.models import UseCase

User = get_user_model()


class UseCasePublishedEditStaysOnlineTests(TestCase):
    """Editing a published UseCase through the real admin form keeps its
    public detail page online, serving the last published values."""

    @classmethod
    def setUpTestData(cls):
        cls.admin_user = User.objects.create_superuser(
            username="uc-admin", email="uc-admin@example.com", password="pw"
        )
        cls.usecase = UseCase.objects.create(status=EditorialWorkflowMixin.STATUS_APPROVED)
        cls.usecase.create_translation(
            "en",
            title="Original Title",
            intro="i",
            body="b",
            outro="o",
            slug="offline-repro-usecase",
            persona="Original Persona",
        )
        cls.usecase.publish(by=cls.admin_user)
        cls.usecase.save()
        # The live-revision marker the admin's publish action sets via
        # core.admin.set_last_published_revision(); visible_on_site() requires
        # it for review/approved objects, exactly as it does for Guide/Prompt.
        UseCase.objects.filter(pk=cls.usecase.pk).update(last_published_revision_id=1)

    def setUp(self):
        self.client.force_login(self.admin_user)

    def _change_url(self):
        return reverse("admin:usecases_usecase_change", args=[self.usecase.pk])

    def _base_payload(self, **overrides):
        u = self.usecase
        data = {
            "author": "",
            "reviewed_by": "",
            "review_note": "",
            "published_at_0": "",
            "published_at_1": "",
            "slug": u.safe_translation_getter("slug", language_code="en"),
            "tools": [],
            "title": u.safe_translation_getter("title", language_code="en"),
            "intro": u.safe_translation_getter("intro", language_code="en"),
            "body": u.safe_translation_getter("body", language_code="en"),
            "outro": u.safe_translation_getter("outro", language_code="en"),
            "_save": "Save",
        }
        data.update(overrides)
        return data

    def test_published_usecase_is_publicly_visible_before_any_edit(self):
        resp = self.client.get("/en/usecases/offline-repro-usecase/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Original Title", resp.content.decode())

    def test_editing_a_field_via_the_real_admin_form_moves_status_to_review(self):
        payload = self._base_payload(title="Changed Title Via Admin")
        resp = self.client.post(self._change_url(), data=payload)
        self.assertEqual(resp.status_code, 302)
        refreshed = UseCase.objects.get(pk=self.usecase.pk)
        self.assertEqual(refreshed.status, EditorialWorkflowMixin.STATUS_REVIEW)

    def test_public_detail_page_stays_online_after_the_admin_edit(self):
        """The Beta 11.7 contract: the previously published page survives the
        edit that moves the object into a new review round."""
        payload = self._base_payload(title="Changed Title Via Admin")
        self.client.post(self._change_url(), data=payload)

        refreshed = UseCase.objects.get(pk=self.usecase.pk)
        self.assertEqual(refreshed.status, EditorialWorkflowMixin.STATUS_REVIEW)
        self.assertEqual(refreshed.live_i18n.get("en", {}).get("title"), "Original Title")

        resp = self.client.get("/en/usecases/offline-repro-usecase/")
        self.assertEqual(resp.status_code, 200)

    def test_public_detail_page_still_shows_the_published_title(self):
        payload = self._base_payload(title="Changed Title Via Admin")
        self.client.post(self._change_url(), data=payload)

        # A fresh, anonymous client: the visitor's view. Reusing the logged-in
        # admin client would carry its "was changed successfully" flash
        # message - which quotes the new title - into the public HTML.
        html = Client().get("/en/usecases/offline-repro-usecase/").content.decode()
        self.assertIn("Original Title", html)
        self.assertNotIn("Changed Title Via Admin", html)

    def test_public_list_page_still_shows_the_published_title(self):
        payload = self._base_payload(title="Changed Title Via Admin")
        self.client.post(self._change_url(), data=payload)

        html = Client().get("/en/usecases/").content.decode()
        self.assertIn("Original Title", html)
        self.assertNotIn("Changed Title Via Admin", html)

    def test_persona_now_has_a_live_snapshot_gate(self):
        """The precondition that made widening the status rule safe: persona
        is snapshotted, so a review-status use case that stays listed renders
        its published persona, never the current draft one."""
        self.assertIn("persona", UseCase.LIVE_SNAPSHOT_FIELDS)

        refreshed = UseCase.objects.get(pk=self.usecase.pk)
        self.assertEqual(refreshed.live_i18n["en"]["persona"], "Original Persona")

        refreshed.set_current_language("en")
        refreshed.persona = "DRAFT PERSONA NEVER PUBLISHED"
        refreshed.save()
        refreshed.move_to_review(by=self.admin_user)
        refreshed.save()

        self.assertTrue(UseCase.objects.visible_on_site().filter(pk=refreshed.pk).exists())

        html = Client().get("/en/usecases/").content.decode()
        self.assertIn("Original Persona", html)
        self.assertNotIn("DRAFT PERSONA NEVER PUBLISHED", html)
