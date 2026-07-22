"""
Beta 11.1: Phase 7 reproduction, KNOWN ISSUE - deliberately not fixed in
this slice.

Reproduces (through the real admin save path, not direct model
manipulation) that editing a published Comparison takes its public detail
page offline: EditorialWorkflowAdminMixin.save_model()'s auto-review guard
(_must_auto_review()/_auto_transition_to_review()) moves any changed,
previously-PUBLISHED object to STATUS_REVIEW by design, so a second pair of
eyes reviews the change before it goes live again. Guide and Prompt stay
publicly visible through this transition because their querysets use
visible_on_site() (published, OR review/approved with an existing live
revision - see core/models/editorial.py::EditorialQuerySet.visible_on_site()).

Comparison (and UseCase, see usecases/tests/test_published_edit_visibility.py)
instead use the strict .published()-only status rule in their own
visible_in_language() (see compare/models.py::ComparisonQuerySet), so the
same edit takes the entire public page offline (404) even though the
previously-published live_i18n snapshot is still intact and unchanged. This
exact status semantics ("published() rather than the broader
visible_on_site()") was already flagged as a deliberate, unchanged decision
in a prior beta (see usecases/tests/test_draft_slug_leak.py); this module
is the first to reproduce it through the real admin POST an editor actually
uses, rather than direct model-level transitions.

This slice does NOT widen Comparison's visibility to visible_on_site(),
because that would introduce a NEW, more severe defect: unlike Guide/
Prompt's fully-snapshotted content, Comparison's tool_entries
(ComparisonToolEntry, one per tool in the comparison) are never
snapshotted at all - compare/views.py's ComparisonDetailView reads
`obj.tool_entries.select_related("tool").all()` directly from the live DB
relation on every request, with no live_i18n equivalent. Under today's
strict published()-only rule that live DB read is harmless: the instant a
tool_entries edit puts the comparison into review, the whole page 404s, so
the one field that isn't snapshotted is exactly the field visibility
already hides from the public. Switching to visible_on_site() would keep
the page online during review/approved and would then serve those
unpublished tool_entries edits directly - a genuine, concretely reproduced
draft leak (see
test_tool_entries_are_never_snapshotted_so_widening_visibility_would_leak_drafts
below), not a hypothetical one.

Properly fixing the offline-on-edit defect needs tool_entries to gain the
same kind of live-snapshot mechanism Comparison's own top-level fields
already have - out of scope for this security-hardening slice; flagged in
the Beta 11.1 final report's "Verschobene Probleme" section as follow-up
workflow/snapshot work.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from catalog.models import Tool
from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin

User = get_user_model()


class ComparisonPublishedEditGoesOfflineKnownIssueTests(TestCase):
    """KNOWN ISSUE reproduction: editing a published Comparison through the
    real admin form 404s its public detail page, even though the live
    snapshot the page would need to render is still fully intact."""

    @classmethod
    def setUpTestData(cls):
        cls.admin_user = User.objects.create_superuser(
            username="cmp-admin", email="cmp-admin@example.com", password="pw"
        )
        cls.comparison = Comparison.objects.create(status=EditorialWorkflowMixin.STATUS_APPROVED)
        cls.comparison.create_translation(
            "en", title="Original Title", intro="i", body="b", slug="offline-repro-comparison"
        )
        cls.comparison.publish(by=cls.admin_user)
        cls.comparison.save()

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

    def test_known_issue_public_detail_page_404s_after_the_admin_edit(self):
        """KNOWN ISSUE (see module docstring): confirmed, reproduced 404 of
        a Comparison whose live_i18n snapshot is still fully intact and
        unchanged. This asserts today's actual, observed behavior - it does
        NOT claim the 404 is the desired or correct contract."""
        payload = self._base_payload(title="Changed Title Via Admin")
        self.client.post(self._change_url(), data=payload)

        refreshed = Comparison.objects.get(pk=self.comparison.pk)
        self.assertEqual(refreshed.live_i18n.get("en", {}).get("title"), "Original Title")

        resp = self.client.get("/en/compare/offline-repro-comparison/")
        self.assertEqual(resp.status_code, 404)

    def test_tool_entries_are_never_snapshotted_so_widening_visibility_would_leak_drafts(self):
        """Concrete mechanism behind the decision NOT to widen Comparison's
        visibility to visible_on_site() in this slice: ComparisonToolEntry
        content has no live-snapshot equivalent at all, so any status that
        keeps the page online while status != published would serve
        whatever is in the DB right now - unpublished edits included."""
        tool = Tool.objects.create(slug="tool-a-offline-repro")
        tool.create_translation("en", name="Tool A")
        entry = self.comparison.tool_entries.create(tool=tool, position=0)
        entry.create_translation("en", summary="DRAFT SUMMARY NEVER PUBLISHED")

        self.assertNotIn("tool_entries", Comparison.LIVE_SNAPSHOT_FIELDS)

        # Simulate what visible_on_site() would additionally allow through:
        # a still-visible review-status object whose tool_entries queryset
        # is read live, unconditionally, by ComparisonDetailView.
        self.comparison.move_to_review(by=self.admin_user)
        self.comparison.last_published_revision_id = 1
        self.comparison.save()

        would_be_visible = Comparison.objects.visible_on_site().filter(pk=self.comparison.pk).exists()
        self.assertTrue(would_be_visible, "visible_on_site() would keep this object public")

        live_entries = self.comparison.tool_entries.select_related("tool").all()
        self.assertEqual(
            live_entries.first().safe_translation_getter("summary", language_code="en"),
            "DRAFT SUMMARY NEVER PUBLISHED",
        )
