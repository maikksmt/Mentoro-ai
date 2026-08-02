"""
Beta 11.9: the regression guard for the defect this module used to merely
reproduce.

History: Beta 11.1 confirmed - through the real admin POST an editor
actually uses, not model-level transitions - that editing a published
Comparison took its public detail page offline. The admin's auto-review
guard (EditorialWorkflowAdminMixin.save_model() -> _must_auto_review() ->
_auto_transition_to_review()) moves any changed, previously-PUBLISHED object
to STATUS_REVIEW by design, and ComparisonQuerySet.visible_in_language() then
used the strict .published()-only status rule, so the whole page 404ed even
though the published live_i18n snapshot was still intact.

Beta 11.1 deliberately did not widen Comparison to visible_on_site(),
because the tool entries had no published representation at all:
compare/views.py read ``obj.tool_entries.select_related("tool").all()``
straight from the live rows on every request, so keeping the page online
during review would have served unreviewed entry edits.

Beta 11.9 closes that first - ``Comparison.live_entries`` freezes the
entries on publish and ``compare/presentation.py`` renders only from it (see
compare/tests/test_live_entries.py) - and only then widens the status rule.
The widened branch additionally requires a non-NULL ``live_entries``, so a
record published before this slice keeps exactly its old behaviour.

This module now asserts the fixed contract: the page stays up, and it keeps
showing the published values.
"""
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from catalog.models import Tool
from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin

User = get_user_model()


class ComparisonPublishedEditStaysOnlineTests(TestCase):
    """Editing a published Comparison through the real admin form keeps its
    public detail page online, serving the last published values."""

    @classmethod
    def setUpTestData(cls):
        cls.admin_user = User.objects.create_superuser(
            username="cmp-admin", email="cmp-admin@example.com", password="pw"
        )
        cls.tool = Tool.objects.create(slug="tool-a-online-repro")
        cls.tool.create_translation("en", name="Tool A")

        cls.comparison = Comparison.objects.create(status=EditorialWorkflowMixin.STATUS_APPROVED)
        cls.comparison.create_translation(
            "en", title="Original Title", intro="i", body="b", slug="offline-repro-comparison"
        )
        entry = cls.comparison.tool_entries.create(tool=cls.tool, position=0)
        entry.create_translation("en", summary="Original entry summary")

        cls.comparison.publish(by=cls.admin_user)
        cls.comparison.save()
        # The live-revision marker the admin's publish action sets via
        # core.admin.set_last_published_revision(); visible_on_site() requires
        # it, exactly as it does for Guide/Prompt/UseCase.
        Comparison.objects.filter(pk=cls.comparison.pk).update(last_published_revision_id=1)

    def setUp(self):
        self.client.force_login(self.admin_user)

    def _change_url(self):
        return reverse("admin:compare_comparison_change", args=[self.comparison.pk])

    def _base_payload(self, **overrides):
        c = self.comparison
        data = {
            "author": "",
            "reviewed_by": "",
            "reviewed_at_0": "",
            "reviewed_at_1": "",
            "review_note": "",
            "title": c.safe_translation_getter("title", language_code="en"),
            "intro": c.safe_translation_getter("intro", language_code="en"),
            "body": c.safe_translation_getter("body", language_code="en"),
            "slug": c.safe_translation_getter("slug", language_code="en"),
            "tool_entries-TOTAL_FORMS": "0",
            "tool_entries-INITIAL_FORMS": "0",
            "tool_entries-MIN_NUM_FORMS": "0",
            "tool_entries-MAX_NUM_FORMS": "1000",
            "_save": "Save",
        }
        data.update(overrides)
        return data

    def test_publishing_wrote_an_entry_snapshot(self):
        """The precondition that made widening the status rule safe."""
        refreshed = Comparison.objects.get(pk=self.comparison.pk)
        self.assertIsNotNone(refreshed.live_entries)
        self.assertEqual(len(refreshed.live_entries), 1)
        self.assertEqual(
            refreshed.live_entries[0]["translations"]["en"]["summary"],
            "Original entry summary",
        )

    def test_published_comparison_is_publicly_visible_before_any_edit(self):
        resp = self.client.get("/en/compare/offline-repro-comparison/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Original Title", resp.content.decode())

    def test_editing_a_field_via_the_real_admin_form_moves_status_to_review(self):
        payload = self._base_payload(title="Changed Title Via Admin")
        resp = self.client.post(self._change_url(), data=payload)
        self.assertEqual(resp.status_code, 302)
        refreshed = Comparison.objects.get(pk=self.comparison.pk)
        self.assertEqual(refreshed.status, EditorialWorkflowMixin.STATUS_REVIEW)

    def test_public_detail_page_stays_online_after_the_admin_edit(self):
        """The Beta 11.9 contract: the previously published page survives the
        edit that moves the object into a new review round."""
        payload = self._base_payload(title="Changed Title Via Admin")
        self.client.post(self._change_url(), data=payload)

        refreshed = Comparison.objects.get(pk=self.comparison.pk)
        self.assertEqual(refreshed.status, EditorialWorkflowMixin.STATUS_REVIEW)
        self.assertEqual(refreshed.live_i18n.get("en", {}).get("title"), "Original Title")

        resp = self.client.get("/en/compare/offline-repro-comparison/")
        self.assertEqual(resp.status_code, 200)

    def test_public_detail_page_still_shows_the_published_title(self):
        payload = self._base_payload(title="Changed Title Via Admin")
        self.client.post(self._change_url(), data=payload)

        # A fresh, anonymous client: the visitor's view. Reusing the logged-in
        # admin client would carry its "was changed successfully" flash
        # message - which quotes the new title - into the public HTML.
        html = Client().get("/en/compare/offline-repro-comparison/").content.decode()
        self.assertIn("Original Title", html)
        self.assertNotIn("Changed Title Via Admin", html)

    def test_tool_entries_are_snapshotted_so_widening_visibility_is_safe(self):
        """The inverse of the Beta 11.1 reproduction: an unreviewed entry
        edit must not reach the public page while the comparison stays
        online through the editorial round."""
        entry = self.comparison.tool_entries.get()
        entry.set_current_language("en")
        entry.summary = "DRAFT SUMMARY NEVER PUBLISHED"
        entry.save()

        refreshed = Comparison.objects.get(pk=self.comparison.pk)
        refreshed.move_to_review(by=self.admin_user)
        refreshed.save()

        self.assertTrue(
            Comparison.objects.visible_on_site().filter(pk=refreshed.pk).exists(),
            "the comparison stays public through the review round",
        )

        html = Client().get("/en/compare/offline-repro-comparison/").content.decode()
        self.assertIn("Original entry summary", html)
        self.assertNotIn("DRAFT SUMMARY NEVER PUBLISHED", html)
