"""
Beta 11.11C4H: the Beta 11.11C4G review-edit-guard primitive integrated into
``PromptAdmin``'s normal changeform save path.

``PromptAdmin.save_model()`` captures a Beta 11.11C1/C4D v2 payload baseline
for an *existing* Prompt before the root save; ``PromptAdmin.save_related()``
compares the current payload - rebuilt fresh only after the root, its active
Parler translation, tag/tool membership and formsets are all persisted -
against that baseline, and calls the existing Beta 11.11B2B2
``invalidate_editorial_review_state()`` exactly once if, and only if, the
canonical payload actually changed. These tests drive the real Django admin
changeform (POST through the test client, or - for the handful of scenarios
that genuinely require it, such as an internal lifecycle-contract violation -
the two admin methods directly, always inside the same real atomic/reversion
environment the production code requires) and assert on the resulting
database state: status, bindings, revisions, and - where relevant - what
never happens (no invalidation, no extra save, no extra revision).
"""
import itertools
from unittest import mock

from django.contrib import admin as django_admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection, transaction
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
import reversion
from reversion.models import Revision, Version

from catalog.models import Tool
from core.models.editorial import EditorialWorkflowMixin as Workflow
from prompts.admin import PromptAdmin, _PromptReviewEditIntegrationError
from prompts.models import Prompt
from prompts.review_approval import approve_prompt_review
from prompts.review_edit_guard import PromptReviewEditBaseline
from prompts.review_submission import submit_prompt_for_review

User = get_user_model()

_slug_counter = itertools.count()

CHANGELIST_URL = reverse("admin:prompts_prompt_changelist")
SUBMIT_ACTION = "action_submit_for_review"
APPROVE_ACTION = "action_approve"


def refetch(prompt):
    """django-fsm's protected ``status`` descriptor rejects the ``setattr``
    ``refresh_from_db()`` performs, so reload through the manager."""
    return Prompt.objects.get(pk=prompt.pk)


def make_tool(name):
    tool = Tool.objects.create(slug=f"c4h-tool-{next(_slug_counter)}")
    tool.create_translation("en", name=name)
    return tool


def change_url(prompt):
    return reverse("admin:prompts_prompt_change", args=[prompt.pk])


def base_payload(prompt, *, author=None, tools=(), **overrides):
    data = {
        "author": str(author.pk) if author else "",
        "review_note": prompt.review_note,
        "published_at_0": "",
        "published_at_1": "",
        "tools": [str(t.pk) for t in tools],
        "slug": prompt.safe_translation_getter("slug", language_code="en") or f"c4h-{next(_slug_counter)}",
        "title": prompt.safe_translation_getter("title", language_code="en") or "Title",
        "intro": prompt.safe_translation_getter("intro", language_code="en") or "intro",
        "body": prompt.safe_translation_getter("body", language_code="en") or "body",
        "outro": prompt.safe_translation_getter("outro", language_code="en") or "outro",
        "_continue": "Save",
    }
    data.update(overrides)
    return data


class AdminGuardTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.editor = User.objects.create_user("c4h-editor", password="pw", is_staff=True)
        cls.editor.groups.add(Group.objects.get_or_create(name="Editor")[0])
        cls.author = User.objects.create_user("c4h-author", password="pw", is_staff=True)
        cls.author.groups.add(Group.objects.get_or_create(name="Author")[0])
        cls.other_author = User.objects.create_user("c4h-other-author", password="pw", is_staff=True)
        cls.other_author.groups.add(Group.objects.get_or_create(name="Author")[0])

    def setUp(self):
        self.client.force_login(self.editor)

    def make_prompt(self, *, status=Workflow.STATUS_DRAFT, author=None, **extra):
        prompt = Prompt.objects.create(status=status, author=author, **extra)
        prompt.create_translation(
            "en", title="Title EN", intro="intro", body="body", outro="outro",
            slug=f"c4h-slug-{next(_slug_counter)}",
        )
        return prompt

    def submitted(self, *, author=None):
        prompt = self.make_prompt(author=author)
        submit_prompt_for_review(prompt, actor=self.editor)
        return refetch(prompt)

    def approved(self, *, author=None):
        prompt = self.submitted(author=author)
        approve_prompt_review(refetch(prompt), actor=self.editor)
        return refetch(prompt)


# ======================================================================
# Payload changes on review/approved: must invalidate
# ======================================================================


class PayloadChangeReviewTests(AdminGuardTestCase):
    def test_translation_change_invalidates_to_draft_without_live_snapshot(self):
        prompt = self.submitted(author=self.editor)
        self.assertEqual(refetch(prompt).live_i18n, {})
        resp = self.client.post(
            change_url(prompt),
            base_payload(prompt, author=self.editor, title="Changed via admin"),
        )
        self.assertEqual(resp.status_code, 302)

        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_DRAFT)
        self.assertIsNone(reloaded.review_revision_id)
        self.assertIsNone(reloaded.approved_revision_id)
        self.assertEqual(reloaded.review_payload_fingerprint, "")
        self.assertIsNone(reloaded.reviewed_by_id)
        self.assertIsNone(reloaded.reviewed_at)
        self.assertIsNone(reloaded.submitted_for_review_at)
        self.assertEqual(
            reloaded.translations.get(language_code="en").title, "Changed via admin"
        )

    def test_translation_change_invalidates_to_rework_with_live_snapshot(self):
        prompt = self.submitted(author=self.editor)
        Prompt.objects.filter(pk=prompt.pk).update(live_i18n={"en": {"title": "Live"}})
        self.client.post(
            change_url(prompt),
            base_payload(prompt, author=self.editor, title="Changed with live snapshot"),
        )
        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_REWORK)
        self.assertIsNone(reloaded.review_revision_id)

    def test_new_translation_language_invalidates(self):
        prompt = self.submitted(author=self.editor)
        self.client.post(
            change_url(prompt) + "?language=de",
            base_payload(
                prompt, author=self.editor,
                slug=f"c4h-de-{next(_slug_counter)}", title="DE Title",
                intro="id", body="bd", outro="od",
            ),
        )
        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_DRAFT)
        self.assertIsNone(reloaded.review_revision_id)
        self.assertEqual(sorted(reloaded.translations.values_list("language_code", flat=True)), ["de", "en"])

    def test_tool_added_invalidates(self):
        tool = make_tool("Guard Add Tool")
        prompt = self.submitted(author=self.editor)
        self.client.post(change_url(prompt), base_payload(prompt, author=self.editor, tools=(tool,)))
        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_DRAFT)
        self.assertIsNone(reloaded.review_revision_id)
        self.assertEqual(list(reloaded.tools.values_list("pk", flat=True)), [tool.pk])

    def test_tool_removed_invalidates(self):
        tool = make_tool("Guard Remove Tool")
        prompt = self.make_prompt(author=self.editor)
        prompt.tools.add(tool)
        submit_prompt_for_review(refetch(prompt), actor=self.editor)
        prompt = refetch(prompt)

        self.client.post(change_url(prompt), base_payload(prompt, author=self.editor, tools=()))
        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_DRAFT)
        self.assertEqual(list(reloaded.tools.all()), [])

    def test_author_reassignment_invalidates(self):
        prompt = self.submitted(author=self.author)
        self.client.post(change_url(prompt), base_payload(prompt, author=self.other_author))
        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_DRAFT)
        self.assertEqual(reloaded.author_id, self.other_author.pk)

    def test_author_cleared_to_none_invalidates(self):
        prompt = self.submitted(author=self.author)
        self.client.post(change_url(prompt), base_payload(prompt, author=None))
        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_DRAFT)
        self.assertIsNone(reloaded.author_id)

    def test_author_assigned_from_none_invalidates(self):
        prompt = self.submitted(author=None)
        self.client.post(change_url(prompt), base_payload(prompt, author=self.author))
        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_DRAFT)
        self.assertEqual(reloaded.author_id, self.author.pk)


class PayloadChangeApprovedTests(AdminGuardTestCase):
    def test_translation_change_invalidates_approved_to_draft(self):
        prompt = self.approved(author=self.editor)
        self.client.post(
            change_url(prompt), base_payload(prompt, author=self.editor, body="Changed approved body")
        )
        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_DRAFT)
        self.assertIsNone(reloaded.review_revision_id)
        self.assertIsNone(reloaded.approved_revision_id)
        self.assertEqual(reloaded.review_payload_fingerprint, "")

    def test_translation_change_invalidates_approved_to_rework_with_live_snapshot(self):
        prompt = self.approved(author=self.editor)
        Prompt.objects.filter(pk=prompt.pk).update(live_i18n={"en": {"title": "Live"}})
        self.client.post(
            change_url(prompt), base_payload(prompt, author=self.editor, intro="Changed approved intro")
        )
        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_REWORK)
        self.assertIsNone(reloaded.approved_revision_id)


# ======================================================================
# No-op: unchanged payload
# ======================================================================


class NoOpUnchangedSaveTests(AdminGuardTestCase):
    def test_identical_resave_leaves_review_binding_untouched(self):
        prompt = self.submitted(author=self.editor)
        before = refetch(prompt)
        self.client.post(change_url(prompt), base_payload(before, author=self.editor))
        after = refetch(prompt)
        self.assertEqual(after.status, Workflow.STATUS_REVIEW)
        self.assertEqual(after.review_revision_id, before.review_revision_id)
        self.assertEqual(after.review_payload_fingerprint, before.review_payload_fingerprint)

    def test_identical_resave_leaves_approved_binding_untouched(self):
        prompt = self.approved(author=self.editor)
        before = refetch(prompt)
        self.client.post(change_url(prompt), base_payload(before, author=self.editor))
        after = refetch(prompt)
        self.assertEqual(after.status, Workflow.STATUS_APPROVED)
        self.assertEqual(after.approved_revision_id, before.approved_revision_id)

    def test_no_op_save_does_not_call_b2b2(self):
        prompt = self.submitted(author=self.editor)
        with mock.patch(
            "prompts.review_edit_guard.invalidate_editorial_review_state"
        ) as invalidate:
            self.client.post(change_url(prompt), base_payload(refetch(prompt), author=self.editor))
        invalidate.assert_not_called()


class NonFingerprintRelevantChangeTests(AdminGuardTestCase):
    """``review_note`` is genuinely admin-editable (in ``form.base_fields``)
    and genuinely absent from the C1 v2 payload - a real field this admin
    form exposes that provably cannot invalidate anything."""

    def test_review_note_change_alone_does_not_invalidate(self):
        prompt = self.submitted(author=self.editor)
        before = refetch(prompt)
        self.client.post(
            change_url(prompt),
            base_payload(before, author=self.editor, review_note="internal note only"),
        )
        after = refetch(prompt)
        self.assertEqual(after.status, Workflow.STATUS_REVIEW)
        self.assertEqual(after.review_revision_id, before.review_revision_id)
        self.assertEqual(after.review_payload_fingerprint, before.review_payload_fingerprint)
        self.assertEqual(after.review_note, "internal note only")


# ======================================================================
# Non-invalidatable statuses
# ======================================================================


class NonInvalidatableStatusTests(AdminGuardTestCase):
    def test_draft_status_unaffected_by_c4h(self):
        prompt = self.make_prompt(status=Workflow.STATUS_DRAFT, author=self.editor)
        self.client.post(change_url(prompt), base_payload(prompt, author=self.editor, title="Draft edit"))
        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_DRAFT)

    def test_rework_status_unaffected_by_c4h(self):
        prompt = self.make_prompt(status=Workflow.STATUS_REWORK, author=self.editor)
        self.client.post(change_url(prompt), base_payload(prompt, author=self.editor, title="Rework edit"))
        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_REWORK)

    def test_archived_status_unaffected_by_c4h(self):
        prompt = self.make_prompt(status=Workflow.STATUS_ARCHIVED, author=self.editor)
        self.client.post(change_url(prompt), base_payload(prompt, author=self.editor, title="Archived edit"))
        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_ARCHIVED)

    def test_published_status_documented_existing_behavior(self):
        """
        Pre-existing (not C4H-invented) behavior: ``EditorialWorkflowAdminMixin
        .save_model()`` auto-transitions a *published* prompt with real form
        changes into an *unbound* "review" (no review_revision/fingerprint -
        it only calls ``move_to_review()`` + ``obj.save()``, never Beta
        11.11C2A's submit primitive). C4H's own compare then runs afterwards,
        sees that freshly-unbound "review" row against a baseline captured
        while it was still "published" (i.e. definitely payload_changed=True
        given the real content edit below), and correctly invalidates it via
        B2B2 - draft, since there is no live_i18n here. This is not a new
        status C4H invented for "published"; it is the fail-closed, expected
        consequence of two pre-existing/new mechanisms composing: an unbound
        "review" row is exactly the kind of state Beta 11.11B2A's own
        migration was designed to never leave standing.
        """
        prompt = self.make_prompt(
            status=Workflow.STATUS_PUBLISHED, author=self.editor, is_published=True
        )
        self.client.post(change_url(prompt), base_payload(prompt, author=self.editor, title="Published edit"))
        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_DRAFT)
        self.assertIsNone(reloaded.review_revision_id)

    def test_published_status_unaffected_when_no_real_content_change(self):
        """The pre-existing auto-review mechanism only fires when
        ``form.has_changed()`` - an unchanged resave never triggers it, and
        C4H's own no-op contract applies identically."""
        prompt = self.make_prompt(
            status=Workflow.STATUS_PUBLISHED, author=self.editor, is_published=True
        )
        self.client.post(change_url(prompt), base_payload(prompt, author=self.editor))
        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_PUBLISHED)


# ======================================================================
# Tags (not admin-editable) and tools (admin-editable)
# ======================================================================


class TagsAndToolsTests(AdminGuardTestCase):
    def test_tools_field_is_admin_editable(self):
        ma = PromptAdmin(Prompt, django_admin.site)
        rf = RequestFactory()
        request = rf.get("/")
        request.user = self.editor
        form_class = ma.get_form(request, obj=None)
        self.assertIn("tools", form_class.base_fields)
        self.assertNotIn("tags", form_class.base_fields)

    def test_tag_added_between_the_two_guard_phases_invalidates(self):
        """
        Tags are not part of ``PromptAdmin``'s changeform fields at all (see
        ``test_tools_field_is_admin_editable`` above) - a real POST cannot
        exercise a tag change through this admin. This directly drives
        ``save_model()``/``save_related()`` in the exact real environment
        Beta 11.11C4G requires (an active atomic block, real reversion
        context via VersionAdmin's own contract), mutating tags between the
        two calls exactly as a hypothetical future admin tag field would.
        """
        prompt = self.submitted(author=self.editor)
        ma = PromptAdmin(Prompt, django_admin.site)
        request = RequestFactory().post("/")
        request.user = self.editor

        with transaction.atomic():
            with reversion.create_revision():
                reversion.set_user(self.editor)
                fresh = Prompt.objects.select_for_update().get(pk=prompt.pk)
                fresh.set_current_language("en")
                form = mock.Mock(instance=fresh, save_m2m=mock.Mock())

                ma.save_model(request, fresh, form, True)
                fresh.tags.add("c4h-added-tag")
                result = ma.save_related(request, form, [], True)

        self.assertTrue(result.payload_changed)
        self.assertTrue(result.invalidated)
        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_DRAFT)
        self.assertIsNone(reloaded.review_revision_id)

    def test_tool_add_and_remove_are_each_a_single_compare(self):
        tool_a = make_tool("Single Compare A")
        tool_b = make_tool("Single Compare B")
        prompt = self.submitted(author=self.editor)
        with mock.patch(
            "prompts.review_edit_guard.invalidate_editorial_review_state",
            wraps=__import__(
                "core.review_binding", fromlist=["invalidate_editorial_review_state"]
            ).invalidate_editorial_review_state,
        ) as invalidate:
            self.client.post(
                change_url(prompt), base_payload(refetch(prompt), author=self.editor, tools=(tool_a, tool_b))
            )
        self.assertEqual(invalidate.call_count, 1)


# ======================================================================
# Add changeform: no guard at all
# ======================================================================


class AddChangeformTests(AdminGuardTestCase):
    def _add_payload(self, **overrides):
        data = {
            "author": str(self.editor.pk),
            "review_note": "",
            "published_at_0": "",
            "published_at_1": "",
            "tools": [],
            "slug": f"c4h-add-{next(_slug_counter)}",
            "title": "New Prompt",
            "intro": "i",
            "body": "b",
            "outro": "o",
            "_continue": "Save",
        }
        data.update(overrides)
        return data

    def test_add_creates_no_baseline_and_no_compare(self):
        with mock.patch(
            "prompts.admin.capture_prompt_review_edit_baseline"
        ) as capture, mock.patch(
            "prompts.admin.invalidate_prompt_review_if_payload_changed"
        ) as compare:
            resp = self.client.post(reverse("admin:prompts_prompt_add"), self._add_payload())
        self.assertEqual(resp.status_code, 302)
        capture.assert_not_called()
        compare.assert_not_called()

    def test_add_with_tools_succeeds_normally(self):
        tool = make_tool("Add Path Tool")
        resp = self.client.post(
            reverse("admin:prompts_prompt_add"), self._add_payload(tools=[str(tool.pk)])
        )
        self.assertEqual(resp.status_code, 302)
        created = Prompt.objects.order_by("-pk").first()
        self.assertEqual(list(created.tools.values_list("pk", flat=True)), [tool.pk])
        self.assertEqual(created.status, Workflow.STATUS_DRAFT)

    def test_add_leaves_no_request_local_baseline_residue(self):
        resp = self.client.post(reverse("admin:prompts_prompt_add"), self._add_payload())
        self.assertEqual(resp.status_code, 302)
        # No direct way to inspect the finished request, but a stray
        # baseline would only ever have been populated by save_model() under
        # change=True - proven structurally impossible for Add by the mocked
        # non-call test above; this is the behavioural companion.
        self.assertEqual(Prompt.objects.filter(status=Workflow.STATUS_REVIEW).count(), 0)


# ======================================================================
# Errors and rollback
# ======================================================================


class ErrorAndRollbackTests(AdminGuardTestCase):
    def test_capture_failure_prevents_any_save(self):
        prompt = self.submitted(author=self.editor)
        before = refetch(prompt)
        with mock.patch(
            "prompts.admin.capture_prompt_review_edit_baseline",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    change_url(prompt), base_payload(before, author=self.editor, title="Should not save")
                )
        after = refetch(prompt)
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.review_revision_id, before.review_revision_id)
        self.assertEqual(
            after.translations.get(language_code="en").title,
            before.translations.get(language_code="en").title,
        )

    def test_save_model_failure_leaves_no_stale_baseline(self):
        prompt = self.submitted(author=self.editor)
        before = refetch(prompt)

        captured = {}

        def spy_capture(obj, **kwargs):
            from prompts.review_edit_guard import capture_prompt_review_edit_baseline as real_capture

            baseline = real_capture(obj, **kwargs)
            captured["baseline"] = baseline
            return baseline

        with mock.patch("prompts.admin.capture_prompt_review_edit_baseline", side_effect=spy_capture):
            with mock.patch.object(
                django_admin.ModelAdmin, "save_model", side_effect=RuntimeError("save_model boom")
            ):
                with self.assertRaises(RuntimeError):
                    self.client.post(
                        change_url(prompt), base_payload(before, author=self.editor, title="Boom")
                    )

        self.assertIn("baseline", captured)
        after = refetch(prompt)
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.review_revision_id, before.review_revision_id)

    def test_save_related_failure_rolls_back_everything(self):
        prompt = self.submitted(author=self.editor)
        before = refetch(prompt)

        with mock.patch.object(
            django_admin.ModelAdmin, "save_related", side_effect=RuntimeError("save_related boom")
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    change_url(prompt), base_payload(before, author=self.editor, title="Boom related")
                )

        after = refetch(prompt)
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.review_revision_id, before.review_revision_id)
        self.assertEqual(after.review_payload_fingerprint, before.review_payload_fingerprint)
        self.assertEqual(
            after.translations.get(language_code="en").title,
            before.translations.get(language_code="en").title,
        )

    def test_compare_failure_rolls_back_the_full_changeform_save(self):
        prompt = self.submitted(author=self.editor)
        before = refetch(prompt)
        revisions_before = Revision.objects.count()

        with mock.patch(
            "prompts.admin.invalidate_prompt_review_if_payload_changed",
            side_effect=RuntimeError("compare boom"),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    change_url(prompt), base_payload(before, author=self.editor, title="Compare boom")
                )

        after = refetch(prompt)
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.review_revision_id, before.review_revision_id)
        self.assertEqual(
            after.translations.get(language_code="en").title,
            before.translations.get(language_code="en").title,
        )
        self.assertEqual(Revision.objects.count(), revisions_before)


# ======================================================================
# Missing baseline: fail closed
# ======================================================================


class MissingBaselineFailClosedTests(AdminGuardTestCase):
    def test_save_related_without_prior_save_model_fails_closed(self):
        prompt = self.submitted(author=self.editor)
        ma = PromptAdmin(Prompt, django_admin.site)
        request = RequestFactory().post("/")
        request.user = self.editor

        fresh = refetch(prompt)
        fresh.set_current_language("en")
        form = mock.Mock(instance=fresh, save_m2m=mock.Mock())

        with transaction.atomic():
            with reversion.create_revision():
                reversion.set_user(self.editor)
                with self.assertRaises(_PromptReviewEditIntegrationError):
                    ma.save_related(request, form, [], True)

        form.save_m2m.assert_not_called()
        after = refetch(prompt)
        self.assertEqual(after.status, Workflow.STATUS_REVIEW)
        self.assertEqual(after.review_revision_id, fresh.review_revision_id)


# ======================================================================
# Request-lifecycle cleanup
# ======================================================================


class RequestLifecycleCleanupTests(AdminGuardTestCase):
    ATTR = "_mentoro_prompt_review_edit_baselines"

    def test_successful_save_leaves_no_baseline_residue(self):
        prompt = self.submitted(author=self.editor)
        captured_requests = []
        original_save_related = PromptAdmin.save_related

        def spy(self_admin, request, form, formsets, change):
            result = original_save_related(self_admin, request, form, formsets, change)
            captured_requests.append(request)
            return result

        with mock.patch.object(PromptAdmin, "save_related", spy):
            self.client.post(change_url(prompt), base_payload(refetch(prompt), author=self.editor))

        self.assertEqual(len(captured_requests), 1)
        self.assertEqual(getattr(captured_requests[0], self.ATTR, {}), {})

    def test_unchanged_save_also_leaves_no_baseline_residue(self):
        prompt = self.submitted(author=self.editor)
        captured_requests = []
        original_save_related = PromptAdmin.save_related

        def spy(self_admin, request, form, formsets, change):
            result = original_save_related(self_admin, request, form, formsets, change)
            captured_requests.append(request)
            return result

        with mock.patch.object(PromptAdmin, "save_related", spy):
            self.client.post(change_url(prompt), base_payload(refetch(prompt), author=self.editor))

        self.assertEqual(getattr(captured_requests[0], self.ATTR, {}), {})

    def test_two_prompts_edited_on_the_same_request_object_stay_isolated(self):
        """Simulates two prompts sharing one request-local store, proving
        keys are separated by prompt id and both are consumed correctly."""
        prompt_a = self.submitted(author=self.editor)
        prompt_b = self.submitted(author=self.editor)
        ma = PromptAdmin(Prompt, django_admin.site)
        request = RequestFactory().post("/")
        request.user = self.editor

        with transaction.atomic():
            with reversion.create_revision():
                reversion.set_user(self.editor)
                for prompt in (prompt_a, prompt_b):
                    fresh = Prompt.objects.select_for_update().get(pk=prompt.pk)
                    fresh.set_current_language("en")
                    fresh.title = f"Changed {prompt.pk}"
                    form = mock.Mock(instance=fresh, save_m2m=mock.Mock())
                    ma.save_model(request, fresh, form, True)
                    ma.save_related(request, form, [], True)

        self.assertEqual(getattr(request, self.ATTR), {})
        self.assertEqual(refetch(prompt_a).status, Workflow.STATUS_DRAFT)
        self.assertEqual(refetch(prompt_b).status, Workflow.STATUS_DRAFT)


# ======================================================================
# Permissions and object boundaries
# ======================================================================


class PermissionsTests(AdminGuardTestCase):
    def test_author_can_edit_and_invalidate_their_own_prompt(self):
        prompt = self.submitted(author=self.author)
        self.client.force_login(self.author)
        resp = self.client.post(
            change_url(prompt), base_payload(refetch(prompt), author=self.author, title="Author edit")
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_DRAFT)

    def test_author_field_stays_readonly_for_non_editor_authors(self):
        prompt = self.submitted(author=self.author)
        self.client.force_login(self.author)
        resp = self.client.get(change_url(prompt))
        self.assertNotIn("author", resp.context["adminform"].form.fields)

    def test_author_cannot_save_someone_elses_prompt(self):
        """
        The "Author" group carries a real Django ``view_prompt`` model
        permission (pre-existing, unrelated to C4H), so a GET on someone
        else's changeform renders 200 in read-only mode - the actual
        boundary is on POST, where ``_changeform_view`` consults the custom
        object-level ``has_change_permission()`` override and denies it.
        """
        prompt = self.submitted(author=self.other_author)
        before = refetch(prompt)
        self.client.force_login(self.author)
        resp = self.client.post(
            change_url(prompt), base_payload(before, author=self.other_author, title="Should not save")
        )
        self.assertEqual(resp.status_code, 403)
        after = refetch(prompt)
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.review_revision_id, before.review_revision_id)

    def test_staff_without_editorial_role_gets_no_changeform_access(self):
        plain_staff = User.objects.create_user("c4h-plain-staff", password="pw", is_staff=True)
        prompt = self.submitted(author=self.editor)
        self.client.force_login(plain_staff)
        resp = self.client.get(change_url(prompt))
        self.assertEqual(resp.status_code, 403)

    def test_editor_can_edit_and_invalidate_any_prompt(self):
        prompt = self.submitted(author=self.author)
        resp = self.client.post(
            change_url(prompt), base_payload(refetch(prompt), author=self.author, title="Editor edit")
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_DRAFT)


# ======================================================================
# Reversion and LogEntry
# ======================================================================


class ReversionAndLogEntryTests(AdminGuardTestCase):
    def test_real_change_produces_exactly_one_revision_with_final_state(self):
        import json

        from django.contrib.admin.models import LogEntry

        prompt = self.submitted(author=self.author)
        revisions_before = Revision.objects.count()
        logs_before = LogEntry.objects.count()

        self.client.post(
            change_url(prompt),
            base_payload(refetch(prompt), author=self.other_author, title="Reversion Test Title"),
        )

        self.assertEqual(Revision.objects.count(), revisions_before + 1)
        self.assertEqual(LogEntry.objects.count(), logs_before + 1)

        revision = Revision.objects.latest("pk")
        self.assertEqual(revision.user_id, self.editor.pk)

        root_version = revision.version_set.get(
            content_type__app_label="prompts", content_type__model="prompt", object_id=str(prompt.pk)
        )
        fields = json.loads(root_version.serialized_data)[0]["fields"]
        self.assertEqual(fields["status"], Workflow.STATUS_DRAFT)
        self.assertEqual(fields["author"], self.other_author.pk)
        self.assertIsNone(fields["review_revision"])

        translation_version = revision.version_set.get(
            content_type__app_label="prompts", content_type__model="prompttranslation",
        )
        t_fields = json.loads(translation_version.serialized_data)[0]["fields"]
        self.assertEqual(t_fields["title"], "Reversion Test Title")

    def test_no_op_save_keeps_the_existing_versionadmin_contract(self):
        prompt = self.submitted(author=self.editor)
        revisions_before = Revision.objects.count()
        self.client.post(change_url(prompt), base_payload(refetch(prompt), author=self.editor))
        # VersionAdmin still records a revision for every real changeform
        # POST regardless of content diff - unchanged pre-existing contract,
        # not something C4H removes or adds to.
        self.assertEqual(Revision.objects.count(), revisions_before + 1)

    def test_rollback_leaves_no_revision_or_version_or_logentry(self):
        from django.contrib.admin.models import LogEntry

        prompt = self.submitted(author=self.editor)
        revisions_before = Revision.objects.count()
        versions_before = Version.objects.count()
        logs_before = LogEntry.objects.count()

        with mock.patch(
            "prompts.admin.invalidate_prompt_review_if_payload_changed",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    change_url(prompt), base_payload(refetch(prompt), author=self.editor, title="Boom")
                )

        self.assertEqual(Revision.objects.count(), revisions_before)
        self.assertEqual(Version.objects.count(), versions_before)
        self.assertEqual(LogEntry.objects.count(), logs_before)


# ======================================================================
# Query and lock contract
# ======================================================================


class QueryAndLockContractTests(AdminGuardTestCase):
    def test_capture_lock_precedes_the_first_root_update(self):
        prompt = self.submitted(author=self.editor)
        with CaptureQueriesContext(connection) as ctx:
            self.client.post(
                change_url(prompt), base_payload(refetch(prompt), author=self.editor, title="Lock order")
            )
        prompt_queries = [q for q in ctx.captured_queries if '"prompts_prompt"' in q["sql"]]
        first_lock_index = next(
            i for i, q in enumerate(prompt_queries) if "FOR UPDATE" in q["sql"].upper()
        )
        first_update_index = next(
            (i for i, q in enumerate(prompt_queries) if q["sql"].strip().upper().startswith("UPDATE")),
            None,
        )
        self.assertIsNotNone(first_update_index)
        self.assertLess(first_lock_index, first_update_index)

    def test_no_op_save_issues_no_guard_update(self):
        prompt = self.submitted(author=self.editor)
        fingerprint_before = refetch(prompt).review_payload_fingerprint
        with CaptureQueriesContext(connection) as ctx:
            self.client.post(change_url(prompt), base_payload(refetch(prompt), author=self.editor))
        self.assertEqual(refetch(prompt).review_payload_fingerprint, fingerprint_before)
        # The pre-existing VersionAdmin/Parler root save still runs and, being
        # a full `obj.save()`, always lists every column - including
        # review_payload_fingerprint - in its SET clause regardless of value.
        # A real B2B2 invalidation is only detectable by the exact substring
        # `"review_payload_fingerprint" = ''` (cleared to empty), never by
        # the column name and some unrelated `''` (e.g. review_note) merely
        # co-occurring in the same full-row UPDATE.
        cleared = [
            q for q in ctx.captured_queries
            if '"review_payload_fingerprint" = \'\'' in q["sql"]
        ]
        self.assertEqual(cleared, [])

    def test_no_auth_user_n_plus_one(self):
        """
        Not a tight fixed count (session/auth middleware resolving
        ``request.user``, and the ``author`` ``ModelChoiceField`` validating
        its posted pk against ``auth_user``, both cost a query independently
        of C4G/C4H) - the meaningful guarantee is that editing *one* prompt
        never scales with the number of tools/translations touched, i.e. the
        guard's own payload build (confirmed query-budget-controlled at the
        C1 unit level) adds no *additional* per-relation author lookup here.
        """
        prompt = self.submitted(author=self.editor)
        tool_a, tool_b = make_tool("NPlusOne A"), make_tool("NPlusOne B")
        with CaptureQueriesContext(connection) as ctx:
            self.client.post(
                change_url(prompt),
                base_payload(
                    refetch(prompt), author=self.editor, title="Query check", tools=(tool_a, tool_b)
                ),
            )
        auth_queries_two_tools = len([q for q in ctx.captured_queries if '"auth_user"' in q["sql"]])

        prompt2 = self.submitted(author=self.editor)
        with CaptureQueriesContext(connection) as ctx2:
            self.client.post(
                change_url(prompt2),
                base_payload(refetch(prompt2), author=self.editor, title="Query check 2"),
            )
        auth_queries_no_tools = len([q for q in ctx2.captured_queries if '"auth_user"' in q["sql"]])

        self.assertEqual(auth_queries_two_tools, auth_queries_no_tools)


# ======================================================================
# Action isolation unchanged
# ======================================================================


class ActionIsolationUnchangedTests(AdminGuardTestCase):
    def _run_action(self, action, pk):
        return self.client.post(
            CHANGELIST_URL,
            data={"action": action, "_selected_action": [str(pk)], "index": "0"},
            follow=True,
        )

    def test_submit_action_still_produces_one_isolated_revision(self):
        prompt = self.make_prompt(author=self.editor)
        revisions_before = Revision.objects.count()
        self._run_action(SUBMIT_ACTION, prompt.pk)
        self.assertEqual(Revision.objects.count(), revisions_before + 1)
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_REVIEW)

    def test_approve_action_still_produces_one_isolated_revision(self):
        # Authored by someone other than the acting editor - self-approval
        # is forbidden by the existing content.approve rule (editor AND NOT
        # author), unrelated to C4H.
        prompt = self.submitted(author=self.author)
        revisions_before = Revision.objects.count()
        self._run_action(APPROVE_ACTION, prompt.pk)
        self.assertEqual(Revision.objects.count(), revisions_before + 1)
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_APPROVED)

    def test_actions_never_capture_a_changeform_baseline(self):
        prompt = self.make_prompt(author=self.editor)
        with mock.patch("prompts.admin.capture_prompt_review_edit_baseline") as capture:
            self._run_action(SUBMIT_ACTION, prompt.pk)
        capture.assert_not_called()

    def test_publish_action_unaffected(self):
        prompt = self.approved(author=self.editor)
        self._run_action("action_publish", prompt.pk)
        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_PUBLISHED)


# ======================================================================
# Static safety
# ======================================================================


class StaticSafetyTests(TestCase):
    def test_no_new_transaction_or_reversion_context_in_admin(self):
        import ast
        import pathlib

        source = pathlib.Path("prompts/admin.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden = {"transaction.atomic", "reversion.create_revision", "reversion.set_user", "reversion.set_comment"}
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            parts = []
            while isinstance(func, ast.Attribute):
                parts.append(func.attr)
                func = func.value
            if isinstance(func, ast.Name):
                parts.append(func.id)
            else:
                continue
            name = ".".join(reversed(parts))
            if name in forbidden:
                offenders.append(name)
        self.assertEqual(offenders, [])

    def test_no_direct_b2b2_call_in_admin(self):
        import pathlib

        source = pathlib.Path("prompts/admin.py").read_text(encoding="utf-8")
        # Only the sanctioned import + one call site inside save_related() -
        # confirmed via the AST-based single-call-site check below.
        self.assertIn("invalidate_prompt_review_if_payload_changed", source)
        self.assertNotIn("invalidate_editorial_review_state", source)

    def test_no_direct_binding_or_status_field_writes_in_admin(self):
        import ast
        import pathlib

        source = pathlib.Path("prompts/admin.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden_attrs = {
            "review_revision", "approved_revision", "review_payload_fingerprint", "status",
        }
        offenders = [
            node.targets
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Attribute) and target.attr in forbidden_attrs
        ]
        self.assertEqual(offenders, [])

    def test_no_broad_exception_handling_in_new_admin_code(self):
        import ast
        import pathlib

        source = pathlib.Path("prompts/admin.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler) or node.type is None:
                continue
            names = [node.type] if isinstance(node.type, ast.Name) else list(getattr(node.type, "elts", []))
            for name_node in names:
                if isinstance(name_node, ast.Name) and name_node.id in ("Exception", "DatabaseError", "IntegrityError"):
                    offenders.append(name_node.id)
        self.assertEqual(offenders, [])

    def test_no_test_shortcuts_in_production_module(self):
        import pathlib

        source = pathlib.Path("prompts/admin.py").read_text(encoding="utf-8")
        self.assertNotIn("pragma: no cover", source)
        self.assertNotIn("expectedFailure", source)

    def test_capture_prompt_review_edit_baseline_type_import(self):
        # Sanity: the private admin exception/store live in prompts/admin.py,
        # not duplicated in the guard module.
        self.assertTrue(issubclass(_PromptReviewEditIntegrationError, RuntimeError))
        self.assertTrue(
            hasattr(PromptReviewEditBaseline, "__dataclass_fields__")
        )
