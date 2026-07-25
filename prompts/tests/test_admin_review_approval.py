"""
Beta 11.11C3B: the prompt admin "Approve (Review → Approved)" action, now
backed by the per-root C3A approval primitive.

The shared editorial admin action wrapped a whole changelist selection in one
transaction and one reversion revision. This module proves ``PromptAdmin``'s
override instead routes each selected prompt through
``approve_prompt_review`` - one atomic transaction, one revision, one binding
per prompt - while preserving the action's name, description, permission
contract and admin UX, and without opening any outer transaction or reversion
context of its own. Guide/UseCase/Comparison keep the shared path.

C3B also extends the Beta 11.11C2B1A ``_selected_action_name``-based
VersionAdmin bypass to cover approval alongside submission: only the actually
selected action (by Django's own ``index`` semantics) grants the bypass, and
that contract is reused verbatim here, never reimplemented.
"""
import ast
import itertools
from unittest import mock

from django.contrib import admin as django_admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from reversion.models import Revision

from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin as Workflow
from core.review_binding import fingerprint_review_payload
from guides.models import Guide
from prompts.models import Prompt
from prompts.review_approval import (
    PromptReviewApprovalError,
    PromptReviewApprovalErrorCode,
    approve_prompt_review,
)
from prompts.review_payload import build_prompt_review_payload
from usecases.models import UseCase

User = get_user_model()

_slug_counter = itertools.count()

SUBMIT_ACTION = "action_submit_for_review"
APPROVE_ACTION = "action_approve"
CHANGELIST_URL = reverse("admin:prompts_prompt_changelist")


def make_prompt(*, status=Workflow.STATUS_DRAFT, author=None, languages=("en",)):
    prompt = Prompt.objects.create(status=status, author=author)
    for language_code in languages:
        prompt.create_translation(
            language_code,
            title=f"Title {language_code}",
            intro="intro",
            body="body",
            outro="outro",
            slug=f"slug-{next(_slug_counter)}",
        )
    return prompt


def refetch(prompt):
    return Prompt.objects.get(pk=prompt.pk)


def message_texts(response):
    return [str(m) for m in response.context["messages"]]


def revision_labels(revision):
    return sorted(
        f"{v.content_type.app_label}.{v.content_type.model}"
        for v in revision.version_set.select_related("content_type")
    )


class PromptAdminApprovalTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.editor = User.objects.create_user(
            "c3b-editor", password="pw", is_staff=True, first_name="Ed", last_name="Itor"
        )
        cls.editor.groups.add(Group.objects.get_or_create(name="Editor")[0])
        cls.other_editor = User.objects.create_user(
            "c3b-editor-2", password="pw", is_staff=True
        )
        cls.other_editor.groups.add(Group.objects.get_or_create(name="Editor")[0])
        cls.author = User.objects.create_user(
            "c3b-author", password="pw", is_staff=True
        )
        cls.author.groups.add(Group.objects.get_or_create(name="Author")[0])

    def setUp(self):
        self.client.force_login(self.editor)

    def post_submit(self, prompts, *, actor_client=None, follow=True):
        client = actor_client or self.client
        return client.post(
            CHANGELIST_URL,
            data={
                "action": SUBMIT_ACTION,
                "_selected_action": [str(p.pk) for p in prompts],
                "index": "0",
            },
            follow=follow,
        )

    def post_approve(self, prompts, *, follow=True, raise_request_exception=True):
        return self.client.post(
            CHANGELIST_URL,
            data={
                "action": APPROVE_ACTION,
                "_selected_action": [str(p.pk) for p in prompts],
                "index": "0",
            },
            follow=follow,
            **({} if raise_request_exception else {"raise_request_exception": False}),
        )

    def make_submitted_prompt(self, *, author=None, languages=("en",)):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=author, languages=languages)
        self.post_submit([prompt])
        return refetch(prompt)


# ======================================================================
# Action identity / registration
# ======================================================================


class ActionRegistrationTests(PromptAdminApprovalTestCase):
    def _actions(self, model):
        from django.test import RequestFactory

        model_admin = django_admin.site._registry[model]
        request = RequestFactory().get("/admin/")
        request.user = User.objects.get(pk=self.editor.pk)
        return model_admin.get_actions(request)

    def test_prompt_admin_has_exactly_one_approve_action(self):
        actions = self._actions(Prompt)
        approve_entries = [name for name in actions if name == APPROVE_ACTION]
        self.assertEqual(approve_entries, [APPROVE_ACTION])

    def test_prompt_admin_still_has_exactly_one_submit_action(self):
        actions = self._actions(Prompt)
        submit_entries = [name for name in actions if "submit_for_review" in name]
        self.assertEqual(submit_entries, [SUBMIT_ACTION])

    def test_prompt_approve_action_resolves_to_the_override(self):
        func = self._actions(Prompt)[APPROVE_ACTION][0]
        self.assertEqual(func.__qualname__, "PromptAdmin.action_approve")

    def test_prompt_approve_action_keeps_its_description(self):
        _func, _name, description = self._actions(Prompt)[APPROVE_ACTION]
        self.assertEqual(str(description), "Approve (Review → Approved)")

    def test_other_editorial_types_keep_the_shared_approve_action(self):
        for model in (Guide, UseCase, Comparison):
            with self.subTest(model=model._meta.label):
                func = self._actions(model)[APPROVE_ACTION][0]
                self.assertEqual(
                    func.__qualname__,
                    "EditorialWorkflowAdminMixin.action_approve",
                )


# ======================================================================
# Phase 12: action dispatch (extends C2B1A, does not duplicate it)
# ======================================================================


class ActionDispatchApprovalTests(PromptAdminApprovalTestCase):
    def _post(self, prompt, *, index, action_values, follow=True):
        return self.client.post(
            CHANGELIST_URL,
            data={
                "action": list(action_values),
                "_selected_action": [str(prompt.pk)],
                "index": index,
            },
            follow=follow,
        )

    def test_approval_selected_upper_bar_is_bypassed_and_succeeds(self):
        prompt = self.make_submitted_prompt(author=self.author)
        revisions_before = Revision.objects.count()
        self._post(prompt, index="0", action_values=[APPROVE_ACTION, "action_publish"])
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_APPROVED)
        self.assertEqual(Revision.objects.count(), revisions_before + 1)

    def test_approval_selected_lower_bar_is_bypassed_and_succeeds(self):
        prompt = self.make_submitted_prompt(author=self.author)
        revisions_before = Revision.objects.count()
        self._post(prompt, index="1", action_values=["action_publish", APPROVE_ACTION])
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_APPROVED)
        self.assertEqual(Revision.objects.count(), revisions_before + 1)

    def test_approval_present_but_submit_selected_runs_submit_not_approval(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author)
        with mock.patch("prompts.admin.approve_prompt_review") as approve_mock:
            self._post(prompt, index="0", action_values=[SUBMIT_ACTION, APPROVE_ACTION])
        approve_mock.assert_not_called()
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_REVIEW)

    def test_approval_present_but_other_action_selected_never_calls_c3a(self):
        prompt = self.make_submitted_prompt(author=self.author)
        with mock.patch("prompts.admin.approve_prompt_review") as approve_mock:
            self._post(prompt, index="1", action_values=[APPROVE_ACTION, "action_publish"])
        approve_mock.assert_not_called()
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_REVIEW)

    def test_submit_still_works_after_the_bypass_extension_upper_bar(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author)
        revisions_before = Revision.objects.count()
        self._post(prompt, index="0", action_values=[SUBMIT_ACTION, APPROVE_ACTION])
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_REVIEW)
        self.assertEqual(Revision.objects.count(), revisions_before + 1)

    def test_submit_still_works_after_the_bypass_extension_lower_bar(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author)
        revisions_before = Revision.objects.count()
        self._post(prompt, index="1", action_values=[APPROVE_ACTION, SUBMIT_ACTION])
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_REVIEW)
        self.assertEqual(Revision.objects.count(), revisions_before + 1)

    def test_other_action_actually_selected_stays_on_versionadmin_path(self):
        prompt = self.make_submitted_prompt(author=self.author)
        with mock.patch("prompts.admin.approve_prompt_review") as approve_mock:
            self._post(prompt, index="0", action_values=["action_publish", APPROVE_ACTION])
        approve_mock.assert_not_called()
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_REVIEW)

    def test_invalid_index_does_not_bypass_approval(self):
        # Unchanged C2B1A contract: negative index fails closed even for a
        # single approve value - proven at the helper level already in
        # test_admin_review_submission.py; here only the approval-specific
        # end-to-end consequence (C3A never reached via the bypass) matters.
        prompt = self.make_submitted_prompt(author=self.author)
        with mock.patch("prompts.admin.approve_prompt_review") as approve_mock:
            self._post(prompt, index="9", action_values=[APPROVE_ACTION, "action_publish"])
        approve_mock.assert_not_called()


# ======================================================================
# Single prompt success
# ======================================================================


class SinglePromptSuccessTests(PromptAdminApprovalTestCase):
    def test_single_approval_full_contract(self):
        prompt = self.make_submitted_prompt(author=self.author, languages=("en", "de"))
        submitted = refetch(prompt)
        review_revision_id = submitted.review_revision_id
        fingerprint = submitted.review_payload_fingerprint
        submitted_at = submitted.submitted_for_review_at

        revisions_before = Revision.objects.count()
        response = self.post_approve([prompt])
        self.assertEqual(response.status_code, 200)

        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_APPROVED)
        self.assertEqual(reloaded.review_revision_id, review_revision_id)
        self.assertEqual(reloaded.approved_revision_id, review_revision_id)
        self.assertEqual(reloaded.review_payload_fingerprint, fingerprint)
        self.assertEqual(reloaded.reviewed_by_id, self.editor.pk)
        self.assertIsNotNone(reloaded.reviewed_at)
        self.assertEqual(reloaded.submitted_for_review_at, submitted_at)

        # exactly one new (approval) revision, actor + comment, graph
        self.assertEqual(Revision.objects.count(), revisions_before + 1)

        approval_revision = Revision.objects.filter(comment="approve").latest("pk")
        self.assertEqual(approval_revision.user_id, self.editor.pk)
        self.assertEqual(set(revision_labels(approval_revision)), {"prompts.prompt", "prompts.prompttranslation"})

        messages_list = message_texts(response)
        self.assertIn("1 prompt was approved.", messages_list)

    def test_no_shared_batch_revision(self):
        prompt = self.make_submitted_prompt(author=self.author)
        self.post_approve([prompt])
        # the approval created its own revision, containing only this root's graph
        approval_revision = Revision.objects.filter(comment="approve").latest("pk")
        self.assertEqual(
            {v.object_id for v in approval_revision.version_set.filter(content_type__model="prompt")},
            {str(prompt.pk)},
        )


# ======================================================================
# Multi-prompt isolation
# ======================================================================


class MultiPromptIsolationTests(PromptAdminApprovalTestCase):
    def test_three_submitted_prompts_get_three_isolated_approval_revisions(self):
        a = self.make_submitted_prompt(author=self.author, languages=("en",))
        b = self.make_submitted_prompt(author=self.author, languages=("en", "de"))
        c = self.make_submitted_prompt(author=self.author, languages=("en",))

        review_ids = {p.pk: refetch(p).review_revision_id for p in (a, b, c)}
        revision_pks_before = set(Revision.objects.values_list("pk", flat=True))

        response = self.post_approve([a, b, c])

        for prompt in (a, b, c):
            self.assertEqual(refetch(prompt).status, Workflow.STATUS_APPROVED)

        # approved_revision_id is bound to the *submit* revision, exactly like
        # review_revision_id (that is the whole point of C3A - the approval
        # audit revision below is never used as the binding).
        approved_ids = {p.pk: refetch(p).approved_revision_id for p in (a, b, c)}
        self.assertEqual(len(set(approved_ids.values())), 3)
        for prompt in (a, b, c):
            self.assertEqual(approved_ids[prompt.pk], review_ids[prompt.pk])

        # exactly three new (approval-audit) revisions, one per root
        new_revision_pks = set(Revision.objects.values_list("pk", flat=True)) - revision_pks_before
        self.assertEqual(len(new_revision_pks), 3)

        audit_revisions_by_root = {}
        for pk in new_revision_pks:
            revision = Revision.objects.get(pk=pk)
            self.assertEqual(revision.comment, "approve")
            self.assertEqual(revision.user_id, self.editor.pk)
            roots = [v.object_id for v in revision.version_set.filter(content_type__model="prompt")]
            self.assertEqual(len(roots), 1)
            audit_revisions_by_root[roots[0]] = revision

        self.assertEqual(set(audit_revisions_by_root), {str(a.pk), str(b.pk), str(c.pk)})

        # no audit revision contains two prompt roots
        for revision in audit_revisions_by_root.values():
            self.assertEqual(revision.version_set.filter(content_type__model="prompt").count(), 1)

        messages_list = message_texts(response)
        self.assertIn("3 prompts were approved.", messages_list)


# ======================================================================
# Mixed status / stale matrix
# ======================================================================


class MixedStatusMatrixTests(PromptAdminApprovalTestCase):
    def test_only_the_unchanged_review_prompt_is_approved(self):
        good = self.make_submitted_prompt(author=self.author)

        stale = self.make_submitted_prompt(author=self.author)
        stale.translations.filter(language_code="en").update(title="Changed after submit")

        others = {
            status: make_prompt(status=status, author=self.author)
            for status in (
                Workflow.STATUS_DRAFT,
                Workflow.STATUS_REWORK,
                Workflow.STATUS_APPROVED,
                Workflow.STATUS_PUBLISHED,
                Workflow.STATUS_ARCHIVED,
            )
        }

        revisions_before = Revision.objects.count()
        response = self.post_approve([good, stale, *others.values()])

        self.assertEqual(refetch(good).status, Workflow.STATUS_APPROVED)

        stale_reloaded = refetch(stale)
        self.assertEqual(stale_reloaded.status, Workflow.STATUS_REVIEW)
        self.assertIsNotNone(stale_reloaded.review_revision_id)
        self.assertIsNone(stale_reloaded.approved_revision_id)

        for status, prompt in others.items():
            reloaded = refetch(prompt)
            self.assertEqual(reloaded.status, status)
            self.assertIsNone(reloaded.approved_revision_id)

        # exactly one new (approval) revision
        self.assertEqual(Revision.objects.count(), revisions_before + 1)

        messages_list = message_texts(response)
        self.assertIn("1 prompt was approved.", messages_list)
        self.assertTrue(
            any("1 prompt was skipped because its reviewed content has changed" in m for m in messages_list),
            messages_list,
        )
        self.assertTrue(
            any("5 prompts were skipped because their status" in m for m in messages_list),
            messages_list,
        )


# ======================================================================
# Stale payload admin path (translation / tag / tool)
# ======================================================================


class StalePayloadAdminTests(PromptAdminApprovalTestCase):
    def _assert_stale_skip(self, prompt):
        before = refetch(prompt)
        revisions_before = Revision.objects.count()
        response = self.post_approve([prompt])
        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_REVIEW)
        self.assertEqual(reloaded.review_revision_id, before.review_revision_id)
        self.assertIsNone(reloaded.approved_revision_id)
        self.assertEqual(Revision.objects.count(), revisions_before)
        self.assertTrue(
            any("reviewed content has changed" in m for m in message_texts(response))
        )

    def test_translation_change_since_submit_is_skipped(self):
        prompt = self.make_submitted_prompt(author=self.author)
        prompt.translations.filter(language_code="en").update(title="Edited")
        self._assert_stale_skip(prompt)

    def test_tag_change_since_submit_is_skipped(self):
        prompt = self.make_submitted_prompt(author=self.author)
        prompt.tags.add("stale-admin-tag")
        self._assert_stale_skip(prompt)

    def test_tool_change_since_submit_is_skipped(self):
        from catalog.models import Tool

        tool = Tool.objects.create(slug="stale-admin-tool")
        tool.create_translation("en", name="Tool")
        prompt = self.make_submitted_prompt(author=self.author)
        prompt.tools.add(tool)
        self._assert_stale_skip(prompt)


# ======================================================================
# Partial failure / independent transactions
# ======================================================================


class PartialFailureTests(PromptAdminApprovalTestCase):
    def test_deleted_middle_prompt_does_not_roll_back_the_others(self):
        a = self.make_submitted_prompt(author=self.author)
        b = self.make_submitted_prompt(author=self.author)
        c = self.make_submitted_prompt(author=self.author)
        revisions_before = Revision.objects.count()

        real_approve = approve_prompt_review

        def wrapper(prompt, **kwargs):
            if prompt.pk == b.pk:
                Prompt.objects.filter(pk=b.pk).delete()
            return real_approve(prompt, **kwargs)

        with mock.patch("prompts.admin.approve_prompt_review", side_effect=wrapper):
            response = self.post_approve([a, b, c])

        self.assertEqual(refetch(a).status, Workflow.STATUS_APPROVED)
        self.assertFalse(Prompt.objects.filter(pk=b.pk).exists())
        self.assertEqual(refetch(c).status, Workflow.STATUS_APPROVED)

        self.assertEqual(Revision.objects.count(), revisions_before + 2)
        self.assertNotEqual(refetch(a).approved_revision_id, refetch(c).approved_revision_id)

        messages_list = message_texts(response)
        self.assertIn("2 prompts were approved.", messages_list)
        self.assertTrue(any("1 selected prompt no longer exists" in m for m in messages_list), messages_list)

    def test_multi_prompt_success_runs_without_mocking_c3a(self):
        a = self.make_submitted_prompt(author=self.author)
        b = self.make_submitted_prompt(author=self.author)
        response = self.post_approve([a, b])
        self.assertEqual(refetch(a).status, Workflow.STATUS_APPROVED)
        self.assertEqual(refetch(b).status, Workflow.STATUS_APPROVED)
        self.assertIn("2 prompts were approved.", message_texts(response))


# ======================================================================
# Corrupt binding
# ======================================================================


class CorruptBindingTests(PromptAdminApprovalTestCase):
    def test_review_revision_pointing_elsewhere_is_not_a_harmless_skip(self):
        a = self.make_submitted_prompt(author=self.author)
        b = self.make_submitted_prompt(author=self.author)
        Prompt.objects.filter(pk=a.pk).update(review_revision_id=refetch(b).review_revision_id)

        revisions_before = Revision.objects.count()
        with self.assertRaises(PromptReviewApprovalError) as ctx:
            self.post_approve([refetch(a)], follow=False)
        self.assertEqual(ctx.exception.code, PromptReviewApprovalErrorCode.REVIEW_BINDING_INVALID)
        self.assertEqual(refetch(a).status, Workflow.STATUS_REVIEW)
        self.assertIsNone(refetch(a).approved_revision_id)
        self.assertEqual(Revision.objects.count(), revisions_before)

    def test_already_set_approved_revision_in_review_status_is_not_a_harmless_skip(self):
        prompt = self.make_submitted_prompt(author=self.author)
        reloaded = refetch(prompt)
        Prompt.objects.filter(pk=prompt.pk).update(approved_revision=reloaded.review_revision)

        with self.assertRaises(PromptReviewApprovalError) as ctx:
            self.post_approve([refetch(prompt)], follow=False)
        self.assertEqual(ctx.exception.code, PromptReviewApprovalErrorCode.REVIEW_BINDING_INVALID)


# ======================================================================
# Permissions
# ======================================================================


class PermissionTests(PromptAdminApprovalTestCase):
    def test_editor_sees_the_action(self):
        self.make_submitted_prompt(author=self.author)
        response = self.client.get(CHANGELIST_URL)
        self.assertContains(response, APPROVE_ACTION)

    def test_editor_can_approve_someone_elses_prompt(self):
        prompt = self.make_submitted_prompt(author=self.author)
        response = self.post_approve([prompt])
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_APPROVED)
        self.assertIn("1 prompt was approved.", message_texts(response))

    def test_editor_cannot_approve_their_own_prompt(self):
        prompt = self.make_submitted_prompt(author=self.editor)
        response = self.post_approve([prompt])
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_REVIEW)
        self.assertIsNone(refetch(prompt).approved_revision_id)
        self.assertTrue(
            any("not allowed to approve" in m for m in message_texts(response))
        )

    def test_author_only_user_cannot_approve_anything(self):
        self.client.force_login(self.author)
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author)
        self.post_submit([prompt], actor_client=self.client)
        response = self.post_approve([prompt])
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_REVIEW)
        self.assertIsNone(refetch(prompt).approved_revision_id)
        self.assertTrue(
            any("not allowed to approve" in m for m in message_texts(response))
        )

    def test_denied_object_never_reaches_c3a(self):
        prompt = self.make_submitted_prompt(author=self.editor)  # editor's own -> denied
        revisions_before = Revision.objects.count()
        with mock.patch("prompts.admin.approve_prompt_review") as approve_mock:
            self.post_approve([prompt])
        approve_mock.assert_not_called()
        self.assertEqual(Revision.objects.count(), revisions_before)
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_REVIEW)

    def test_unprivileged_user_cannot_use_the_action(self):
        # Submit as the editor first - the outsider below has neither submit
        # nor approve permission, so the prompt must already be in review
        # before switching the client to the unprivileged user.
        prompt = self.make_submitted_prompt(author=self.author)
        outsider = User.objects.create_user("c3b-outsider", password="pw", is_staff=True)
        self.client.force_login(outsider)
        revisions_before = Revision.objects.count()
        with mock.patch("prompts.admin.approve_prompt_review") as approve_mock:
            self.post_approve([prompt], raise_request_exception=False)
        approve_mock.assert_not_called()
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_REVIEW)
        self.assertEqual(Revision.objects.count(), revisions_before)


# ======================================================================
# Error classification
# ======================================================================


class ErrorClassificationTests(PromptAdminApprovalTestCase):
    def _raise_code(self, code):
        def side_effect(prompt, **kwargs):
            raise PromptReviewApprovalError(code, f"forced {code}")

        return side_effect

    def test_config_error_stops_processing_and_shows_generic_message(self):
        a = self.make_submitted_prompt(author=self.author)
        b = self.make_submitted_prompt(author=self.author)
        c = self.make_submitted_prompt(author=self.author)

        real_approve = approve_prompt_review
        seen = []

        def side_effect(prompt, **kwargs):
            seen.append(prompt.pk)
            if prompt.pk == b.pk:
                raise PromptReviewApprovalError(
                    PromptReviewApprovalErrorCode.INVALID_DATABASE_ALIAS, "bad alias"
                )
            return real_approve(prompt, **kwargs)

        with mock.patch("prompts.admin.approve_prompt_review", side_effect=side_effect):
            response = self.post_approve([a, b, c])

        self.assertEqual(seen, [a.pk, b.pk])
        self.assertEqual(refetch(a).status, Workflow.STATUS_APPROVED)
        self.assertEqual(refetch(c).status, Workflow.STATUS_REVIEW)

        messages_list = message_texts(response)
        self.assertIn("1 prompt was approved.", messages_list)
        self.assertTrue(any("server configuration problem" in m for m in messages_list))
        for m in messages_list:
            self.assertNotIn("invalid_database_alias", m)
            self.assertNotIn("alias", m.lower())

    def test_status_not_approvable_is_a_recoverable_skip(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author)
        with mock.patch(
            "prompts.admin.approve_prompt_review",
            side_effect=self._raise_code(PromptReviewApprovalErrorCode.STATUS_NOT_APPROVABLE),
        ):
            response = self.post_approve([prompt])
        self.assertTrue(any("status does not allow approval" in m for m in message_texts(response)))

    def test_review_payload_changed_is_a_recoverable_skip(self):
        prompt = self.make_submitted_prompt(author=self.author)
        with mock.patch(
            "prompts.admin.approve_prompt_review",
            side_effect=self._raise_code(PromptReviewApprovalErrorCode.REVIEW_PAYLOAD_CHANGED),
        ):
            response = self.post_approve([prompt])
        self.assertTrue(any("reviewed content has changed" in m for m in message_texts(response)))

    def test_object_not_found_is_a_recoverable_skip(self):
        prompt = self.make_submitted_prompt(author=self.author)
        with mock.patch(
            "prompts.admin.approve_prompt_review",
            side_effect=self._raise_code(PromptReviewApprovalErrorCode.OBJECT_NOT_FOUND),
        ):
            response = self.post_approve([prompt])
        self.assertTrue(any("no longer exists" in m for m in message_texts(response)))

    def test_review_binding_invalid_is_reraised_not_swallowed(self):
        prompt = self.make_submitted_prompt(author=self.author)
        with mock.patch(
            "prompts.admin.approve_prompt_review",
            side_effect=self._raise_code(PromptReviewApprovalErrorCode.REVIEW_BINDING_INVALID),
        ):
            with self.assertRaises(PromptReviewApprovalError) as ctx:
                self.post_approve([prompt], follow=False)
        self.assertEqual(ctx.exception.code, PromptReviewApprovalErrorCode.REVIEW_BINDING_INVALID)

    def test_payload_changed_during_approval_is_reraised(self):
        prompt = self.make_submitted_prompt(author=self.author)
        with mock.patch(
            "prompts.admin.approve_prompt_review",
            side_effect=self._raise_code(
                PromptReviewApprovalErrorCode.PAYLOAD_CHANGED_DURING_APPROVAL
            ),
        ):
            with self.assertRaises(PromptReviewApprovalError):
                self.post_approve([prompt], follow=False)

    def test_active_reversion_context_is_reraised(self):
        prompt = self.make_submitted_prompt(author=self.author)
        with mock.patch(
            "prompts.admin.approve_prompt_review",
            side_effect=self._raise_code(PromptReviewApprovalErrorCode.ACTIVE_REVERSION_CONTEXT),
        ):
            with self.assertRaises(PromptReviewApprovalError):
                self.post_approve([prompt], follow=False)

    def test_integrity_error_keeps_earlier_successes_committed(self):
        a = self.make_submitted_prompt(author=self.author)
        b = self.make_submitted_prompt(author=self.author)
        real_approve = approve_prompt_review

        def side_effect(prompt, **kwargs):
            if prompt.pk == b.pk:
                raise PromptReviewApprovalError(
                    PromptReviewApprovalErrorCode.REVIEW_BINDING_INVALID, "boom"
                )
            return real_approve(prompt, **kwargs)

        with mock.patch("prompts.admin.approve_prompt_review", side_effect=side_effect):
            with self.assertRaises(PromptReviewApprovalError):
                self.post_approve([a, b], follow=False)
        self.assertEqual(refetch(a).status, Workflow.STATUS_APPROVED)


# ======================================================================
# QuerySet stability + exactly one C3A call per object
# ======================================================================


class QuerySetStabilityTests(PromptAdminApprovalTestCase):
    def test_every_selected_id_is_processed_exactly_once_in_pk_order(self):
        prompts = [self.make_submitted_prompt(author=self.author) for _ in range(4)]
        real_approve = approve_prompt_review
        called_pks = []

        def recorder(prompt, **kwargs):
            called_pks.append(prompt.pk)
            return real_approve(prompt, **kwargs)

        with mock.patch("prompts.admin.approve_prompt_review", side_effect=recorder):
            self.post_approve(prompts)

        expected = sorted(p.pk for p in prompts)
        self.assertEqual(called_pks, expected)
        self.assertEqual(len(called_pks), len(set(called_pks)))

    def test_status_change_from_a_success_does_not_drop_later_ids(self):
        prompts = [self.make_submitted_prompt(author=self.author) for _ in range(3)]
        self.post_approve(prompts)
        for prompt in prompts:
            self.assertEqual(refetch(prompt).status, Workflow.STATUS_APPROVED)

    def test_mixed_selection_calls_c3a_for_every_object_including_non_approvable(self):
        review_prompt = self.make_submitted_prompt(author=self.author)
        draft_prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.author)
        called_pks = []
        real_approve = approve_prompt_review

        def recorder(prompt, **kwargs):
            called_pks.append(prompt.pk)
            return real_approve(prompt, **kwargs)

        with mock.patch("prompts.admin.approve_prompt_review", side_effect=recorder):
            self.post_approve([review_prompt, draft_prompt])

        self.assertEqual(sorted(called_pks), sorted([review_prompt.pk, draft_prompt.pk]))


# ======================================================================
# No duplicated C3A logic (static AST)
# ======================================================================


class NoDuplicatedLogicTests(TestCase):
    def _admin_tree(self):
        import pathlib

        import prompts.admin as admin_module

        source = pathlib.Path(admin_module.__file__).read_text(encoding="utf-8")
        return ast.parse(source)

    def test_admin_does_not_call_forbidden_c3a_internals(self):
        """
        Scoped to ``PromptAdmin.action_approve`` itself (mirroring
        ``test_admin_approve_action_opens_no_transaction_or_reversion_context``
        right below), not the whole ``prompts/admin.py`` file: Beta 11.11C4J
        legitimately calls several of these same B2B1/B2B2/C1 primitives
        elsewhere in this file (``_invalidate_reverted_prompt_if_binding_invalid()``/
        ``_finish_prompt_review_guard()``), for the unrelated purpose of
        detecting and fail-closed invalidating a stale binding a
        django-reversion revert/recover restored - never to duplicate what
        ``action_approve`` itself does. What this test must still prove is
        that ``action_approve`` specifically never reaches for any of them.
        """
        forbidden = {
            "create_revision",
            "set_user",
            "set_comment",
            "add_to_revision",
            "build_prompt_review_payload",
            "fingerprint_review_payload",
            "validate_review_binding",
            "validate_approved_binding",
            "revision_contains_object",
            "invalidate_editorial_review_state",
            "approve",  # Prompt.approve / EditorialWorkflowMixin.approve FSM method
        }
        tree = self._admin_tree()
        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "PromptAdmin":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "action_approve":
                        target = item
        self.assertIsNotNone(target)

        offenders = []
        for node in ast.walk(target):
            if isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                if name in forbidden:
                    offenders.append(name)
        self.assertEqual(offenders, [])

    def test_admin_approve_action_opens_no_transaction_or_reversion_context(self):
        tree = self._admin_tree()
        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "PromptAdmin":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "action_approve":
                        target = item
        self.assertIsNotNone(target)

        offenders = []
        for node in ast.walk(target):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and node.func.attr in {
                    "atomic",
                    "create_revision",
                }:
                    offenders.append(node.func.attr)
        self.assertEqual(offenders, [])

    def test_admin_uses_only_the_public_c3a_surface(self):
        import pathlib

        import prompts.admin as admin_module

        source = pathlib.Path(admin_module.__file__).read_text(encoding="utf-8")
        self.assertIn("approve_prompt_review", source)
        self.assertIn("PromptReviewApprovalError", source)


# ======================================================================
# Other editorial types unchanged
# ======================================================================


class OtherEditorialTypesUnchangedTests(TestCase):
    def test_other_admins_do_not_import_the_approval_primitive(self):
        import compare.admin
        import guides.admin
        import usecases.admin

        for module in (guides.admin, usecases.admin, compare.admin):
            source = open(module.__file__, encoding="utf-8").read()
            with self.subTest(module=module.__name__):
                self.assertNotIn("approve_prompt_review", source)
                self.assertNotIn("review_approval", source)


# ======================================================================
# No further runtime activation
# ======================================================================


class NoFurtherActivationTests(PromptAdminApprovalTestCase):
    def test_publish_remains_unguarded(self):
        prompt = self.make_submitted_prompt(author=self.author)
        self.post_approve([prompt])
        self.client.post(
            CHANGELIST_URL,
            data={"action": "action_publish", "_selected_action": [str(prompt.pk)], "index": "0"},
            follow=True,
        )
        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_PUBLISHED)
        # publish still does not check the approved_revision binding at all

    def test_review_note_stays_unchanged_after_approval(self):
        prompt = self.make_submitted_prompt(author=self.author)
        Prompt.objects.filter(pk=prompt.pk).update(review_note="keep me")
        self.post_approve([refetch(prompt)])
        self.assertEqual(refetch(prompt).review_note, "keep me")

    def test_approval_content_matches_the_c1_payload(self):
        prompt = self.make_submitted_prompt(author=self.author)
        expected_fp = fingerprint_review_payload(build_prompt_review_payload(refetch(prompt)))
        self.post_approve([prompt])
        self.assertEqual(refetch(prompt).review_payload_fingerprint, expected_fp)
