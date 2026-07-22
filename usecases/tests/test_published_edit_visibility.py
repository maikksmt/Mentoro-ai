"""
Beta 11.1: Phase 7 reproduction, KNOWN ISSUE - deliberately not fixed in
this slice.

Reproduces (through the real admin save path, not direct model
manipulation) that editing a published UseCase takes its public detail page
offline: EditorialWorkflowAdminMixin.save_model()'s auto-review guard
(_must_auto_review()/_auto_transition_to_review()) moves any changed,
previously-PUBLISHED object to STATUS_REVIEW by design, so a second pair of
eyes reviews the change before it goes live again. Guide and Prompt stay
publicly visible through this transition because their querysets use
visible_on_site() (published, OR review/approved with an existing live
revision - see core/models/editorial.py::EditorialQuerySet.visible_on_site()).

UseCase (and Comparison, see
compare/tests/test_published_edit_visibility.py) instead uses the strict
.published()-only status rule in its own visible_in_language() (see
usecases/models.py::UseCaseQuerySet), so the same edit takes the entire
public page offline (404) even though the previously-published live_i18n
snapshot is still intact and unchanged. This exact status semantics
("published() rather than the broader visible_on_site()") was already
flagged as a deliberate, unchanged decision in a prior beta (see
usecases/tests/test_draft_slug_leak.py's module docstring); this module is
the first to reproduce it through the real admin POST an editor actually
uses, rather than direct model-level transitions.

This slice does NOT widen UseCase's visibility to visible_on_site(),
because that would introduce a NEW, more severe defect: UseCase.persona is
NOT in UseCase.LIVE_SNAPSHOT_FIELDS:

    LIVE_SNAPSHOT_FIELDS = ("slug", "public_slug", "title", "intro", "body", "outro")

and, unlike title/intro/body/outro (all rendered through display_*
properties backed by get_live_value()), templates/usecases/list.html
renders the use case card's persona label via `obj.persona` directly - the
current draft translation value, with no live_i18n/snapshot involvement
whatsoever (see partials/_editorial_card.html's `{% if persona %}` block).
Under today's strict published()-only rule that live read is harmless: the
instant any edit puts the use case into review, it drops out of
UseCaseListView's queryset entirely, so the one field with zero snapshot
gate is exactly the field visibility already hides from the public.
Switching to visible_on_site() would keep such an object in the public list
during review/approved and would then render whatever `persona` currently
holds - a genuine, concretely reproduced draft-leak mechanism (see
test_persona_is_read_live_on_the_list_page_with_no_snapshot_gate_at_all
below), not a hypothetical one. (persona is not itself part of
UseCaseAdmin's editable fieldsets today, so this is not independently
reachable through the current form - but nothing at the model/DB level
prevents it, exactly as with the slug divergence
usecases/tests/test_draft_slug_leak.py documents for the same reason.)

Properly fixing the offline-on-edit defect needs persona to gain the same
kind of live-snapshot mechanism UseCase's other translated fields already
have - out of scope for this security-hardening slice; flagged in the Beta
11.1 final report's "Verschobene Probleme" section as follow-up
workflow/snapshot work.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models.editorial import EditorialWorkflowMixin
from usecases.models import UseCase

User = get_user_model()


class UseCasePublishedEditGoesOfflineKnownIssueTests(TestCase):
    """KNOWN ISSUE reproduction: editing a published UseCase through the
    real admin form 404s its public detail page, even though the live
    snapshot the page would need to render is still fully intact."""

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

    def test_known_issue_public_detail_page_404s_after_the_admin_edit(self):
        """KNOWN ISSUE (see module docstring): confirmed, reproduced 404 of
        a UseCase whose live_i18n snapshot is still fully intact and
        unchanged. This asserts today's actual, observed behavior - it does
        NOT claim the 404 is the desired or correct contract."""
        payload = self._base_payload(title="Changed Title Via Admin")
        self.client.post(self._change_url(), data=payload)

        refreshed = UseCase.objects.get(pk=self.usecase.pk)
        self.assertEqual(refreshed.live_i18n.get("en", {}).get("title"), "Original Title")

        resp = self.client.get("/en/usecases/offline-repro-usecase/")
        self.assertEqual(resp.status_code, 404)

    def test_persona_is_read_live_on_the_list_page_with_no_snapshot_gate_at_all(self):
        """Concrete mechanism behind the decision NOT to widen UseCase's
        visibility to visible_on_site() in this slice: persona has no
        live-snapshot equivalent at all and templates/usecases/list.html
        reads it directly off the object (`obj.persona`), so any status
        that keeps a use case in the public list while status != published
        would render whatever persona is currently in the DB - unpublished
        edits included."""
        self.assertNotIn("persona", UseCase.LIVE_SNAPSHOT_FIELDS)

        u = self.usecase
        u.persona = "DRAFT PERSONA NEVER PUBLISHED"
        u.save()

        # Simulate what visible_on_site() would additionally allow through:
        # a still-visible review-status object with an existing live
        # revision, exactly like Guide/Prompt already permit.
        u.move_to_review(by=self.admin_user)
        u.last_published_revision_id = 1
        u.save()

        would_be_visible = UseCase.objects.visible_on_site().filter(pk=u.pk).exists()
        self.assertTrue(would_be_visible, "visible_on_site() would keep this object public")

        self.assertEqual(u.persona, "DRAFT PERSONA NEVER PUBLISHED")
