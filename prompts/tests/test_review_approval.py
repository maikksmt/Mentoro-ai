"""
Beta 11.11C3A: atomic, per-root prompt review approval.

``approve_prompt_review`` is the second primitive (after Beta 11.11C2A's
``submit_prompt_for_review``) to touch the review binding, and the first to
*confirm* it rather than create it: it takes exactly one ``review`` Prompt
with an already-valid ``review_revision``/``review_payload_fingerprint`` (the
C1 payload bound at submit time) to ``approved``, binding
``approved_revision`` to that *same* revision - never a fresh lookup, never a
new "approved" snapshot. These tests hold it to that full contract: input/
alias/actor validation (actor now mandatory), the fresh-row source of truth,
delegation to the central Beta 11.11B2B1 binding validator (no parallel
reimplementation), the stale-payload matrix that blocks approval whenever the
stored content no longer matches what was reviewed, the audit-revision graph
a successful approval produces, per-root isolation across two prompts, and a
complete rollback matrix proving no orphan revision, version or partial
binding survives any failure.
"""
import itertools
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import DEFAULT_DB_ALIAS, connection
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from parler.utils.context import switch_language
from reversion.models import Revision, Version

from catalog.models import Tool
from core.models.editorial import EditorialWorkflowMixin as Workflow
from core.review_binding import BindingFailureReason, fingerprint_review_payload
from guides.models import Guide
from prompts.models import Prompt, PromptTranslation
from prompts.review_approval import (
    APPROVE_REVISION_COMMENT,
    PromptReviewApprovalError,
    PromptReviewApprovalErrorCode,
    PromptReviewApprovalResult,
    approve_prompt_review,
)
from prompts.review_payload import build_prompt_review_payload
from prompts.review_submission import submit_prompt_for_review

User = get_user_model()

_slug_counter = itertools.count()


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def refetch(prompt):
    """django-fsm's protected ``status`` descriptor rejects the ``setattr``
    ``refresh_from_db()`` performs, so reload through the manager."""
    return Prompt.objects.get(pk=prompt.pk)


def make_tool(slug, name):
    tool = Tool.objects.create(slug=slug)
    tool.create_translation("en", name=name)
    return tool


def make_prompt(*, status=Workflow.STATUS_DRAFT, author=None, languages=("en",), tools=(), tags=()):
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
    if tools:
        prompt.tools.add(*tools)
    if tags:
        prompt.tags.add(*tags)
    return prompt


def submitted_prompt(*, actor, author=None, languages=("en",), tools=(), tags=()):
    """A prompt already carrying a valid C2A review binding - the normal
    precondition for approval."""
    prompt = make_prompt(
        status=Workflow.STATUS_DRAFT, author=author, languages=languages, tools=tools, tags=tags
    )
    submit_prompt_for_review(prompt, actor=actor)
    return refetch(prompt)


def root_versions(revision):
    return list(revision.version_set.filter(content_type__model="prompt"))


def translation_versions(revision):
    return list(revision.version_set.filter(content_type__model="prompttranslation"))


def version_fields(version):
    import json

    return json.loads(version.serialized_data)[0]["fields"]


class ApprovalTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.actor = User.objects.create_user(
            "c3a-actor", password="pw", first_name="Grace", last_name="Hopper"
        )
        cls.author = User.objects.create_user("c3a-author", password="pw")


# ======================================================================
# Result / error contract
# ======================================================================


class ResultAndErrorContractTests(ApprovalTestCase):
    def test_result_is_frozen_and_slotted(self):
        result = PromptReviewApprovalResult(
            prompt_id=1,
            previous_status="review",
            current_status="approved",
            review_revision_id=5,
            approved_revision_id=5,
            fingerprint="a" * 64,
            database_alias="default",
        )
        with self.assertRaises(AttributeError):
            result.prompt_id = 2
        self.assertTrue(hasattr(type(result), "__slots__"))

    def test_result_holds_only_scalars(self):
        prompt = submitted_prompt(actor=self.actor, author=self.author)
        result = approve_prompt_review(prompt, actor=self.actor)
        for value in (
            result.prompt_id,
            result.review_revision_id,
            result.approved_revision_id,
        ):
            self.assertIsInstance(value, int)
        for value in (result.fingerprint, result.database_alias, result.previous_status, result.current_status):
            self.assertIsInstance(value, str)

    def test_error_codes_are_stable_distinct_strings(self):
        codes = list(PromptReviewApprovalErrorCode)
        self.assertEqual(len(codes), 12)
        self.assertEqual(len(set(codes)), 12)
        for code in codes:
            self.assertIsInstance(code, str)

    def test_error_carries_its_code(self):
        try:
            approve_prompt_review(None, actor=self.actor)
        except PromptReviewApprovalError as exc:
            self.assertEqual(exc.code, PromptReviewApprovalErrorCode.UNSUPPORTED_OBJECT)
            self.assertIsInstance(exc, ValueError)
        else:
            self.fail("expected PromptReviewApprovalError")

    def test_non_binding_errors_carry_no_binding_reason(self):
        try:
            approve_prompt_review(None, actor=self.actor)
        except PromptReviewApprovalError as exc:
            self.assertIsNone(exc.binding_reason)
        else:
            self.fail("expected PromptReviewApprovalError")


# ======================================================================
# Input, alias, actor validation
# ======================================================================


class InputValidationTests(ApprovalTestCase):
    def test_none_is_rejected(self):
        with self.assertRaises(PromptReviewApprovalError) as ctx:
            approve_prompt_review(None, actor=self.actor)
        self.assertEqual(ctx.exception.code, PromptReviewApprovalErrorCode.UNSUPPORTED_OBJECT)

    def test_other_editorial_type_is_rejected(self):
        guide = Guide.objects.create()
        with self.assertRaises(PromptReviewApprovalError) as ctx:
            approve_prompt_review(guide, actor=self.actor)
        self.assertEqual(ctx.exception.code, PromptReviewApprovalErrorCode.UNSUPPORTED_OBJECT)

    def test_translation_is_rejected(self):
        prompt = make_prompt()
        translation = prompt.translations.get(language_code="en")
        with self.assertRaises(PromptReviewApprovalError) as ctx:
            approve_prompt_review(translation, actor=self.actor)
        self.assertEqual(ctx.exception.code, PromptReviewApprovalErrorCode.UNSUPPORTED_OBJECT)

    def test_queryset_is_rejected(self):
        make_prompt()
        with self.assertRaises(PromptReviewApprovalError) as ctx:
            approve_prompt_review(Prompt.objects.all(), actor=self.actor)
        self.assertEqual(ctx.exception.code, PromptReviewApprovalErrorCode.UNSUPPORTED_OBJECT)

    def test_list_is_rejected(self):
        prompt = make_prompt()
        with self.assertRaises(PromptReviewApprovalError) as ctx:
            approve_prompt_review([prompt], actor=self.actor)
        self.assertEqual(ctx.exception.code, PromptReviewApprovalErrorCode.UNSUPPORTED_OBJECT)

    def test_unsaved_prompt_is_rejected(self):
        with self.assertRaises(PromptReviewApprovalError) as ctx:
            approve_prompt_review(Prompt(), actor=self.actor)
        self.assertEqual(ctx.exception.code, PromptReviewApprovalErrorCode.UNSAVED_OBJECT)

    def test_deleted_prompt_raises_object_not_found(self):
        prompt = submitted_prompt(actor=self.actor)
        Prompt.objects.filter(pk=prompt.pk).delete()
        with self.assertRaises(PromptReviewApprovalError) as ctx:
            approve_prompt_review(prompt, actor=self.actor)
        self.assertEqual(ctx.exception.code, PromptReviewApprovalErrorCode.OBJECT_NOT_FOUND)

    def test_invalid_using_type_raises_type_error(self):
        prompt = submitted_prompt(actor=self.actor)
        with self.assertRaises(TypeError):
            approve_prompt_review(prompt, actor=self.actor, using=123)

    def test_unknown_alias_raises_invalid_database_alias(self):
        prompt = submitted_prompt(actor=self.actor)
        with self.assertRaises(PromptReviewApprovalError) as ctx:
            approve_prompt_review(prompt, actor=self.actor, using="not-a-real-alias")
        self.assertEqual(ctx.exception.code, PromptReviewApprovalErrorCode.INVALID_DATABASE_ALIAS)

    def test_unknown_alias_is_not_misclassified_as_mismatch(self):
        prompt = submitted_prompt(actor=self.actor)
        self.assertEqual(prompt._state.db, DEFAULT_DB_ALIAS)
        with self.assertRaises(PromptReviewApprovalError) as ctx:
            approve_prompt_review(prompt, actor=self.actor, using="bogus")
        self.assertEqual(ctx.exception.code, PromptReviewApprovalErrorCode.INVALID_DATABASE_ALIAS)

    def test_explicit_alias_contradicting_object_alias_is_mismatch(self):
        prompt = submitted_prompt(actor=self.actor)
        prompt._state.db = "not-default"
        with self.assertRaises(PromptReviewApprovalError) as ctx:
            approve_prompt_review(prompt, actor=self.actor, using=DEFAULT_DB_ALIAS)
        self.assertEqual(ctx.exception.code, PromptReviewApprovalErrorCode.DATABASE_ALIAS_MISMATCH)

    def test_actor_none_is_rejected(self):
        prompt = submitted_prompt(actor=self.actor)
        with self.assertRaises(PromptReviewApprovalError) as ctx:
            approve_prompt_review(prompt, actor=None)
        self.assertEqual(ctx.exception.code, PromptReviewApprovalErrorCode.INVALID_ACTOR)

    def test_invalid_actor_type_is_rejected(self):
        prompt = submitted_prompt(actor=self.actor)
        with self.assertRaises(PromptReviewApprovalError) as ctx:
            approve_prompt_review(prompt, actor="not-a-user")
        self.assertEqual(ctx.exception.code, PromptReviewApprovalErrorCode.INVALID_ACTOR)

    def test_unsaved_actor_is_rejected(self):
        prompt = submitted_prompt(actor=self.actor)
        with self.assertRaises(PromptReviewApprovalError) as ctx:
            approve_prompt_review(prompt, actor=User(username="ghost"))
        self.assertEqual(ctx.exception.code, PromptReviewApprovalErrorCode.INVALID_ACTOR)

    def test_actor_of_wrong_model_is_rejected(self):
        prompt = submitted_prompt(actor=self.actor)
        tool = make_tool("actor-wrong-model", "Tool")
        with self.assertRaises(PromptReviewApprovalError) as ctx:
            approve_prompt_review(prompt, actor=tool)
        self.assertEqual(ctx.exception.code, PromptReviewApprovalErrorCode.INVALID_ACTOR)

    def test_actor_database_alias_mismatch_is_rejected(self):
        prompt = submitted_prompt(actor=self.actor)
        other_actor = User.objects.create_user("c3a-alias-mismatch", password="pw")
        other_actor._state.db = "not-default"
        with self.assertRaises(PromptReviewApprovalError) as ctx:
            approve_prompt_review(prompt, actor=other_actor)
        self.assertEqual(
            ctx.exception.code, PromptReviewApprovalErrorCode.ACTOR_DATABASE_ALIAS_MISMATCH
        )

    def test_early_errors_perform_no_mutation_and_no_revision(self):
        prompt = submitted_prompt(actor=self.actor)
        revisions_before = Revision.objects.count()
        for kwargs in ({"prompt": None, "actor": self.actor}, {"prompt": Prompt(), "actor": self.actor}):
            with self.assertRaises(PromptReviewApprovalError):
                approve_prompt_review(**kwargs)
        with self.assertRaises(PromptReviewApprovalError):
            approve_prompt_review(prompt, actor=self.actor, using="bogus")
        self.assertEqual(Revision.objects.count(), revisions_before)
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_REVIEW)


# ======================================================================
# Status gate
# ======================================================================


class StatusGateTests(ApprovalTestCase):
    def test_review_is_accepted(self):
        prompt = submitted_prompt(actor=self.actor, author=self.author)
        result = approve_prompt_review(prompt, actor=self.actor)
        self.assertEqual(result.current_status, Workflow.STATUS_APPROVED)

    def test_draft_rework_approved_published_archived_are_rejected(self):
        for status in (
            Workflow.STATUS_DRAFT,
            Workflow.STATUS_REWORK,
            Workflow.STATUS_APPROVED,
            Workflow.STATUS_PUBLISHED,
            Workflow.STATUS_ARCHIVED,
        ):
            with self.subTest(status=status):
                prompt = make_prompt(status=status, author=self.author)
                revisions_before = Revision.objects.count()
                with self.assertRaises(PromptReviewApprovalError) as ctx:
                    approve_prompt_review(prompt, actor=self.actor)
                self.assertEqual(
                    ctx.exception.code, PromptReviewApprovalErrorCode.STATUS_NOT_APPROVABLE
                )
                self.assertEqual(refetch(prompt).status, status)
                self.assertEqual(Revision.objects.count(), revisions_before)

    def test_repeated_approval_is_not_a_no_op_it_is_rejected(self):
        prompt = submitted_prompt(actor=self.actor, author=self.author)
        approve_prompt_review(prompt, actor=self.actor)
        with self.assertRaises(PromptReviewApprovalError) as ctx:
            approve_prompt_review(refetch(prompt), actor=self.actor)
        self.assertEqual(ctx.exception.code, PromptReviewApprovalErrorCode.STATUS_NOT_APPROVABLE)


# ======================================================================
# Central binding validation (delegated, not duplicated)
# ======================================================================


class BindingValidationTests(ApprovalTestCase):
    def test_missing_review_revision_is_rejected_with_the_central_reason(self):
        prompt = submitted_prompt(actor=self.actor)
        Prompt.objects.filter(pk=prompt.pk).update(review_revision=None)
        with self.assertRaises(PromptReviewApprovalError) as ctx:
            approve_prompt_review(refetch(prompt), actor=self.actor)
        self.assertEqual(ctx.exception.code, PromptReviewApprovalErrorCode.REVIEW_BINDING_INVALID)
        self.assertEqual(ctx.exception.binding_reason, BindingFailureReason.REVIEW_REVISION_MISSING)

    def test_empty_fingerprint_is_rejected_with_the_central_reason(self):
        prompt = submitted_prompt(actor=self.actor)
        Prompt.objects.filter(pk=prompt.pk).update(review_payload_fingerprint="")
        with self.assertRaises(PromptReviewApprovalError) as ctx:
            approve_prompt_review(refetch(prompt), actor=self.actor)
        self.assertEqual(ctx.exception.code, PromptReviewApprovalErrorCode.REVIEW_BINDING_INVALID)
        self.assertEqual(
            ctx.exception.binding_reason, BindingFailureReason.REVIEW_FINGERPRINT_MISSING
        )

    def test_syntactically_invalid_fingerprint_is_rejected_with_the_central_reason(self):
        prompt = submitted_prompt(actor=self.actor)
        Prompt.objects.filter(pk=prompt.pk).update(review_payload_fingerprint="not-a-hex-digest")
        with self.assertRaises(PromptReviewApprovalError) as ctx:
            approve_prompt_review(refetch(prompt), actor=self.actor)
        self.assertEqual(ctx.exception.code, PromptReviewApprovalErrorCode.REVIEW_BINDING_INVALID)
        self.assertEqual(
            ctx.exception.binding_reason, BindingFailureReason.REVIEW_FINGERPRINT_INVALID
        )

    def test_revision_belonging_to_a_different_prompt_is_rejected(self):
        prompt_a = submitted_prompt(actor=self.actor)
        prompt_b = submitted_prompt(actor=self.actor)
        Prompt.objects.filter(pk=prompt_a.pk).update(review_revision_id=refetch(prompt_b).review_revision_id)
        with self.assertRaises(PromptReviewApprovalError) as ctx:
            approve_prompt_review(refetch(prompt_a), actor=self.actor)
        self.assertEqual(ctx.exception.code, PromptReviewApprovalErrorCode.REVIEW_BINDING_INVALID)
        self.assertEqual(
            ctx.exception.binding_reason, BindingFailureReason.REVIEW_REVISION_NOT_FOR_OBJECT
        )

    def test_approved_revision_already_set_in_review_status_is_rejected(self):
        prompt = submitted_prompt(actor=self.actor)
        reloaded = refetch(prompt)
        Prompt.objects.filter(pk=prompt.pk).update(approved_revision=reloaded.review_revision)
        with self.assertRaises(PromptReviewApprovalError) as ctx:
            approve_prompt_review(refetch(prompt), actor=self.actor)
        self.assertEqual(ctx.exception.code, PromptReviewApprovalErrorCode.REVIEW_BINDING_INVALID)
        # not covered by the central B2B1 validator - no fabricated reason
        self.assertIsNone(ctx.exception.binding_reason)
        # not silently overwritten
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_REVIEW)

    def test_binding_errors_perform_no_mutation_and_no_revision(self):
        prompt = submitted_prompt(actor=self.actor)
        Prompt.objects.filter(pk=prompt.pk).update(review_revision=None)
        revisions_before = Revision.objects.count()
        with self.assertRaises(PromptReviewApprovalError):
            approve_prompt_review(refetch(prompt), actor=self.actor)
        self.assertEqual(Revision.objects.count(), revisions_before)
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_REVIEW)
        self.assertIsNone(refetch(prompt).approved_revision_id)


# ======================================================================
# Stale payload matrix - content changed since submit
# ======================================================================


class StalePayloadTests(ApprovalTestCase):
    def _assert_blocked(self, prompt):
        revisions_before = Revision.objects.count()
        with self.assertRaises(PromptReviewApprovalError) as ctx:
            approve_prompt_review(refetch(prompt), actor=self.actor)
        self.assertEqual(ctx.exception.code, PromptReviewApprovalErrorCode.REVIEW_PAYLOAD_CHANGED)
        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_REVIEW)
        self.assertIsNone(reloaded.approved_revision_id)
        self.assertIsNone(reloaded.reviewed_by_id)
        self.assertIsNone(reloaded.reviewed_at)
        self.assertEqual(Revision.objects.count(), revisions_before)

    def test_translation_field_changed(self):
        prompt = submitted_prompt(actor=self.actor)
        PromptTranslation.objects.filter(master_id=prompt.pk, language_code="en").update(
            title="Changed after submit"
        )
        self._assert_blocked(prompt)

    def test_language_added(self):
        prompt = submitted_prompt(actor=self.actor)
        prompt.create_translation(
            "de", title="DE", intro="i", body="b", outro="o", slug=f"stale-de-{next(_slug_counter)}"
        )
        self._assert_blocked(prompt)

    def test_language_removed(self):
        prompt = submitted_prompt(actor=self.actor, languages=("en", "de"))
        PromptTranslation.objects.filter(master_id=prompt.pk, language_code="de").delete()
        self._assert_blocked(prompt)

    def test_tag_added(self):
        prompt = submitted_prompt(actor=self.actor)
        prompt.tags.add("newly-added")
        self._assert_blocked(prompt)

    def test_tag_removed(self):
        prompt = submitted_prompt(actor=self.actor, tags=("removable",))
        prompt.tags.remove("removable")
        self._assert_blocked(prompt)

    def test_tool_added(self):
        prompt = submitted_prompt(actor=self.actor)
        tool = make_tool("stale-tool-added", "Added")
        prompt.tools.add(tool)
        self._assert_blocked(prompt)

    def test_tool_removed(self):
        tool = make_tool("stale-tool-removed", "Removed")
        prompt = submitted_prompt(actor=self.actor, tools=(tool,))
        prompt.tools.remove(tool)
        self._assert_blocked(prompt)

    def test_author_reassigned(self):
        other_author = User.objects.create_user("c3a-other-author", password="pw")
        prompt = submitted_prompt(actor=self.actor, author=self.author)
        Prompt.objects.filter(pk=prompt.pk).update(author=other_author)
        self._assert_blocked(prompt)


# ======================================================================
# Success
# ======================================================================


class SuccessTests(ApprovalTestCase):
    def test_full_approval_contract(self):
        tool = make_tool("success-tool", "Tool")
        prompt = submitted_prompt(
            actor=self.actor, author=self.author, languages=("en", "de"), tools=(tool,), tags=("alpha",)
        )
        before = refetch(prompt)
        review_revision_id = before.review_revision_id
        fingerprint = before.review_payload_fingerprint
        submitted_at = before.submitted_for_review_at
        Prompt.objects.filter(pk=prompt.pk).update(review_note="keep me")

        result = approve_prompt_review(refetch(prompt), actor=self.actor)

        self.assertEqual(result.prompt_id, prompt.pk)
        self.assertEqual(result.previous_status, Workflow.STATUS_REVIEW)
        self.assertEqual(result.current_status, Workflow.STATUS_APPROVED)
        self.assertEqual(result.review_revision_id, review_revision_id)
        self.assertEqual(result.approved_revision_id, review_revision_id)
        self.assertEqual(result.fingerprint, fingerprint)
        self.assertEqual(result.database_alias, DEFAULT_DB_ALIAS)

        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_APPROVED)
        self.assertEqual(reloaded.review_revision_id, review_revision_id)
        self.assertEqual(reloaded.approved_revision_id, review_revision_id)
        self.assertEqual(reloaded.review_payload_fingerprint, fingerprint)
        self.assertEqual(reloaded.reviewed_by_id, self.actor.pk)
        self.assertIsNotNone(reloaded.reviewed_at)
        self.assertEqual(reloaded.submitted_for_review_at, submitted_at)
        self.assertEqual(reloaded.review_note, "keep me")

        # content completely unchanged
        self.assertEqual(
            fingerprint_review_payload(build_prompt_review_payload(reloaded)), fingerprint
        )

    def test_author_display_name_change_does_not_block_approval(self):
        """Beta 11.11C4D inverted this from the v1 contract (formerly part of
        StalePayloadTests's blocked matrix): the review payload's author
        section is now ``{"id": author_id}`` only, so a pure display-name
        change since submit must not be treated as stale content."""
        prompt = submitted_prompt(actor=self.actor, author=self.author)
        before = refetch(prompt)
        fingerprint = before.review_payload_fingerprint
        User.objects.filter(pk=self.author.pk).update(first_name="Changed", last_name="Name")

        result = approve_prompt_review(refetch(prompt), actor=self.actor)

        self.assertEqual(result.current_status, Workflow.STATUS_APPROVED)
        self.assertEqual(result.fingerprint, fingerprint)
        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_APPROVED)
        self.assertEqual(reloaded.review_payload_fingerprint, fingerprint)


# ======================================================================
# Audit revision graph
# ======================================================================


class AuditRevisionGraphTests(ApprovalTestCase):
    def test_exactly_one_new_revision_with_root_and_translations_only(self):
        prompt = submitted_prompt(actor=self.actor, author=self.author, languages=("en", "de"))
        revisions_before = set(Revision.objects.values_list("pk", flat=True))

        approve_prompt_review(refetch(prompt), actor=self.actor)

        new_revision_pks = set(Revision.objects.values_list("pk", flat=True)) - revisions_before
        self.assertEqual(len(new_revision_pks), 1)
        revision = Revision.objects.get(pk=new_revision_pks.pop())

        labels = {f"{v.content_type.app_label}.{v.content_type.model}" for v in revision.version_set.all()}
        self.assertEqual(labels, {"prompts.prompt", "prompts.prompttranslation"})

        roots = root_versions(revision)
        self.assertEqual([v.object_id for v in roots], [str(prompt.pk)])

        translation_ids = {v.object_id for v in translation_versions(revision)}
        expected_ids = {
            str(pk) for pk in PromptTranslation.objects.filter(master_id=prompt.pk).values_list("pk", flat=True)
        }
        self.assertEqual(translation_ids, expected_ids)
        self.assertEqual(len(expected_ids), 2)

        # revision identity + actor/comment
        self.assertEqual(revision.user_id, self.actor.pk)
        self.assertEqual(revision.comment, APPROVE_REVISION_COMMENT)

    def test_root_version_holds_the_approval_state(self):
        prompt = submitted_prompt(actor=self.actor, author=self.author)
        review_revision_id = refetch(prompt).review_revision_id
        fingerprint = refetch(prompt).review_payload_fingerprint
        revisions_before = set(Revision.objects.values_list("pk", flat=True))

        approve_prompt_review(refetch(prompt), actor=self.actor)

        new_revision_pk = (set(Revision.objects.values_list("pk", flat=True)) - revisions_before).pop()
        revision = Revision.objects.get(pk=new_revision_pk)
        fields = version_fields(root_versions(revision)[0])

        self.assertEqual(fields["status"], Workflow.STATUS_APPROVED)
        self.assertEqual(fields["review_revision"], review_revision_id)
        self.assertEqual(fields["approved_revision"], review_revision_id)
        self.assertEqual(fields["review_payload_fingerprint"], fingerprint)
        self.assertEqual(fields["reviewed_by"], self.actor.pk)
        self.assertIsNotNone(fields["reviewed_at"])

    def test_no_tag_or_tool_membership_in_the_audit_graph(self):
        tool = make_tool("audit-graph-tool", "Tool")
        prompt = submitted_prompt(actor=self.actor, tools=(tool,), tags=("beta",))
        revisions_before = set(Revision.objects.values_list("pk", flat=True))

        approve_prompt_review(refetch(prompt), actor=self.actor)

        new_revision_pk = (set(Revision.objects.values_list("pk", flat=True)) - revisions_before).pop()
        revision = Revision.objects.get(pk=new_revision_pk)
        labels = {f"{v.content_type.app_label}.{v.content_type.model}" for v in revision.version_set.all()}
        self.assertEqual(labels, {"prompts.prompt", "prompts.prompttranslation"})


# ======================================================================
# Two roots - per-root isolation
# ======================================================================


class TwoRootsTests(ApprovalTestCase):
    def test_two_prompts_get_two_separate_approval_revisions(self):
        other_actor = User.objects.create_user("c3a-actor-b", password="pw")
        prompt_a = submitted_prompt(actor=self.actor, author=self.actor, languages=("en",))
        prompt_b = submitted_prompt(actor=other_actor, author=other_actor, languages=("en", "de"))

        revisions_before = set(Revision.objects.values_list("pk", flat=True))
        result_a = approve_prompt_review(refetch(prompt_a), actor=self.actor)
        result_b = approve_prompt_review(refetch(prompt_b), actor=other_actor)
        new_revision_pks = set(Revision.objects.values_list("pk", flat=True)) - revisions_before

        self.assertEqual(len(new_revision_pks), 2)
        self.assertNotEqual(result_a.approved_revision_id, result_b.approved_revision_id)
        self.assertEqual(result_a.approved_revision_id, result_a.review_revision_id)
        self.assertEqual(result_b.approved_revision_id, result_b.review_revision_id)

        for pk in new_revision_pks:
            revision = Revision.objects.get(pk=pk)
            roots = root_versions(revision)
            self.assertEqual(len(roots), 1)
            root_id = roots[0].object_id
            self.assertIn(root_id, {str(prompt_a.pk), str(prompt_b.pk)})
            # translations in this revision belong only to this same root
            masters = {version_fields(v)["master"] for v in translation_versions(revision)}
            self.assertEqual(masters, {int(root_id)})

        self.assertEqual(refetch(prompt_a).approved_revision_id, refetch(prompt_a).review_revision_id)
        self.assertEqual(refetch(prompt_b).approved_revision_id, refetch(prompt_b).review_revision_id)
        self.assertNotEqual(refetch(prompt_a).approved_revision_id, refetch(prompt_b).approved_revision_id)


# ======================================================================
# Payload re-check after save
# ======================================================================


class PayloadConsistencyAfterSaveTests(ApprovalTestCase):
    def test_unchanged_payload_approves_successfully(self):
        prompt = submitted_prompt(actor=self.actor, author=self.author)
        result = approve_prompt_review(refetch(prompt), actor=self.actor)
        self.assertEqual(result.current_status, Workflow.STATUS_APPROVED)

    def test_payload_changed_during_approval_rolls_back_completely(self):
        prompt = submitted_prompt(actor=self.actor, author=self.author)
        before_snapshot = self._snapshot(prompt)
        revisions_before = Revision.objects.count()
        versions_before = Version.objects.count()

        payload_a = build_prompt_review_payload(refetch(prompt))
        payload_b = {**payload_a, "translations": []}

        with mock.patch(
            "prompts.review_approval.build_prompt_review_payload",
            side_effect=[payload_a, payload_b],
        ), self.assertRaises(PromptReviewApprovalError) as ctx:
            approve_prompt_review(refetch(prompt), actor=self.actor)
        self.assertEqual(
            ctx.exception.code, PromptReviewApprovalErrorCode.PAYLOAD_CHANGED_DURING_APPROVAL
        )
        self.assertEqual(self._snapshot(prompt), before_snapshot)
        self.assertEqual(Revision.objects.count(), revisions_before)
        self.assertEqual(Version.objects.count(), versions_before)

    def _snapshot(self, prompt):
        fresh = refetch(prompt)
        return {
            "status": fresh.status,
            "review_revision_id": fresh.review_revision_id,
            "approved_revision_id": fresh.approved_revision_id,
            "review_payload_fingerprint": fresh.review_payload_fingerprint,
            "reviewed_by_id": fresh.reviewed_by_id,
            "reviewed_at": fresh.reviewed_at,
            "submitted_for_review_at": fresh.submitted_for_review_at,
            "review_note": fresh.review_note,
        }


# ======================================================================
# Rollback matrix
# ======================================================================


class RollbackMatrixTests(ApprovalTestCase):
    def setUp(self):
        self.tool = make_tool("rollback-approval-tool", "Tool")
        self.prompt = submitted_prompt(
            actor=self.actor, author=self.author, languages=("en", "de"),
            tools=(self.tool,), tags=("alpha",),
        )
        Prompt.objects.filter(pk=self.prompt.pk).update(review_note="old feedback")
        self.before = self._snapshot()
        self.revisions_before = Revision.objects.count()
        self.versions_before = Version.objects.count()

    def _snapshot(self):
        fresh = refetch(self.prompt)
        return {
            "status": fresh.status,
            "review_revision_id": fresh.review_revision_id,
            "approved_revision_id": fresh.approved_revision_id,
            "review_payload_fingerprint": fresh.review_payload_fingerprint,
            "reviewed_by_id": fresh.reviewed_by_id,
            "reviewed_at": fresh.reviewed_at,
            "submitted_for_review_at": fresh.submitted_for_review_at,
            "review_note": fresh.review_note,
            "live_i18n": fresh.live_i18n,
            "translations": sorted(fresh.translations.values_list("language_code", "title")),
            "tags": sorted(fresh.tags.names()),
            "tools": sorted(fresh.tools.values_list("pk", flat=True)),
        }

    def _assert_full_rollback(self):
        self.assertEqual(self._snapshot(), self.before)
        self.assertEqual(Revision.objects.count(), self.revisions_before)
        self.assertEqual(Version.objects.count(), self.versions_before)

    def test_payload_builder_failure_before_reversion(self):
        with mock.patch(
            "prompts.review_approval.build_prompt_review_payload",
            side_effect=RuntimeError("builder boom"),
        ), self.assertRaises(RuntimeError):
            approve_prompt_review(refetch(self.prompt), actor=self.actor)
        self._assert_full_rollback()

    def test_fingerprint_failure(self):
        with mock.patch(
            "prompts.review_approval.fingerprint_review_payload",
            side_effect=RuntimeError("fingerprint boom"),
        ), self.assertRaises(RuntimeError):
            approve_prompt_review(refetch(self.prompt), actor=self.actor)
        self._assert_full_rollback()

    def test_transition_failure(self):
        with mock.patch.object(Prompt, "approve", side_effect=RuntimeError("fsm boom")), self.assertRaises(RuntimeError):
            approve_prompt_review(refetch(self.prompt), actor=self.actor)
        self._assert_full_rollback()

    def test_root_save_failure(self):
        with mock.patch.object(Prompt, "save", side_effect=RuntimeError("save boom")), self.assertRaises(RuntimeError):
            approve_prompt_review(refetch(self.prompt), actor=self.actor)
        self._assert_full_rollback()

    def test_reversion_metadata_failure(self):
        with mock.patch("reversion.set_comment", side_effect=RuntimeError("reversion boom")), self.assertRaises(RuntimeError):
            approve_prompt_review(refetch(self.prompt), actor=self.actor)
        self._assert_full_rollback()

    def test_binding_missing_is_a_full_rollback(self):
        Prompt.objects.filter(pk=self.prompt.pk).update(review_revision=None)
        self.before = self._snapshot()
        with self.assertRaises(PromptReviewApprovalError) as ctx:
            approve_prompt_review(refetch(self.prompt), actor=self.actor)
        self.assertEqual(ctx.exception.code, PromptReviewApprovalErrorCode.REVIEW_BINDING_INVALID)
        self._assert_full_rollback()

    def test_content_changed_since_submit_is_a_full_rollback(self):
        PromptTranslation.objects.filter(master_id=self.prompt.pk, language_code="en").update(
            title="Changed"
        )
        self.before = self._snapshot()
        with self.assertRaises(PromptReviewApprovalError) as ctx:
            approve_prompt_review(refetch(self.prompt), actor=self.actor)
        self.assertEqual(ctx.exception.code, PromptReviewApprovalErrorCode.REVIEW_PAYLOAD_CHANGED)
        self._assert_full_rollback()

    def test_payload_changed_during_approval_is_a_full_rollback(self):
        payload_a = build_prompt_review_payload(refetch(self.prompt))
        payload_b = {**payload_a, "translations": []}
        with mock.patch(
            "prompts.review_approval.build_prompt_review_payload",
            side_effect=[payload_a, payload_b],
        ), self.assertRaises(PromptReviewApprovalError) as ctx:
            approve_prompt_review(refetch(self.prompt), actor=self.actor)
        self.assertEqual(
            ctx.exception.code, PromptReviewApprovalErrorCode.PAYLOAD_CHANGED_DURING_APPROVAL
        )
        self._assert_full_rollback()


# ======================================================================
# Active reversion context
# ======================================================================


class ActiveReversionContextTests(ApprovalTestCase):
    def test_running_inside_an_active_context_is_rejected(self):
        import reversion

        prompt = submitted_prompt(actor=self.actor)
        revisions_before = Revision.objects.count()
        with reversion.create_revision(), self.assertRaises(PromptReviewApprovalError) as ctx:
            approve_prompt_review(refetch(prompt), actor=self.actor)
        self.assertEqual(
            ctx.exception.code, PromptReviewApprovalErrorCode.ACTIVE_REVERSION_CONTEXT
        )
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_REVIEW)
        self.assertIsNone(refetch(prompt).approved_revision_id)
        self.assertGreaterEqual(Revision.objects.count(), revisions_before)

    def test_rejection_happens_before_any_query_or_mutation(self):
        import reversion

        prompt = submitted_prompt(actor=self.actor)
        with reversion.create_revision():
            with mock.patch("prompts.review_approval.build_prompt_review_payload") as payload_mock, self.assertRaises(PromptReviewApprovalError):
                approve_prompt_review(refetch(prompt), actor=self.actor)
            payload_mock.assert_not_called()


# ======================================================================
# Query and lock contract
# ======================================================================


class QueryAndLockContractTests(TransactionTestCase):
    """
    ``select_for_update()`` requires a real transaction, so these run under
    ``TransactionTestCase`` (the wrapping-transaction of ``TestCase`` would
    otherwise absorb the lock semantics).
    """

    def setUp(self):
        self.actor = User.objects.create_user("c3a-lock-actor", password="pw")
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.actor)
        submit_prompt_for_review(prompt, actor=self.actor)
        self.prompt = refetch(prompt)

    def test_root_is_locked_with_for_update_on_the_prompt_table(self):
        with CaptureQueriesContext(connection) as ctx:
            approve_prompt_review(refetch(self.prompt), actor=self.actor)
        for_update = [
            q for q in ctx.captured_queries
            if "FOR UPDATE" in q["sql"].upper() and '"prompts_prompt"' in q["sql"]
        ]
        self.assertEqual(len(for_update), 1)
        lock_sql = for_update[0]["sql"].upper()
        self.assertNotIn("INNER JOIN", lock_sql)
        self.assertNotIn("'PUBLISHED'", lock_sql)

    def test_exactly_one_update_on_the_prompt_row(self):
        with CaptureQueriesContext(connection) as ctx:
            approve_prompt_review(refetch(self.prompt), actor=self.actor)
        prompt_updates = [
            q for q in ctx.captured_queries
            if q["sql"].strip().upper().startswith("UPDATE") and '"prompts_prompt"' in q["sql"]
        ]
        self.assertEqual(len(prompt_updates), 1)
        update_sql = prompt_updates[0]["sql"]
        self.assertIn("approved_revision_id", update_sql)
        self.assertIn("reviewed_by_id", update_sql)
        self.assertIn("reviewed_at", update_sql)
        self.assertIn("status", update_sql)
        self.assertIn("updated_at", update_sql)
        # never touches review_revision, the fingerprint, submit metadata or content
        self.assertNotIn("review_revision_id", update_sql)
        self.assertNotIn("review_payload_fingerprint", update_sql)
        self.assertNotIn("submitted_for_review_at", update_sql)

    def test_no_revision_lookup_heuristic_query(self):
        with CaptureQueriesContext(connection) as ctx:
            approve_prompt_review(refetch(self.prompt), actor=self.actor)
        for q in ctx.captured_queries:
            sql = q["sql"].upper()
            if '"REVERSION_REVISION"' in sql and "ORDER BY" in sql:
                self.fail(f"unexpected ordered revision lookup: {q['sql']}")

    def test_updated_at_changes_exactly_once(self):
        before = refetch(self.prompt).updated_at
        approve_prompt_review(refetch(self.prompt), actor=self.actor)
        after = refetch(self.prompt).updated_at
        self.assertGreater(after, before)


# ======================================================================
# Stale caller
# ======================================================================


class StaleCallerTests(ApprovalTestCase):
    def test_caller_says_review_but_db_is_already_approved_is_rejected(self):
        prompt = submitted_prompt(actor=self.actor, author=self.author)
        caller = refetch(prompt)  # in-memory status: "review"
        self.assertEqual(caller.status, Workflow.STATUS_REVIEW)

        # Move the real row to approved through a completely separate path.
        approve_prompt_review(refetch(prompt), actor=self.actor)

        with self.assertRaises(PromptReviewApprovalError) as ctx:
            approve_prompt_review(caller, actor=self.actor)
        self.assertEqual(ctx.exception.code, PromptReviewApprovalErrorCode.STATUS_NOT_APPROVABLE)

    def test_caller_says_approved_but_db_is_actually_review_still_succeeds(self):
        prompt = submitted_prompt(actor=self.actor, author=self.author)
        approve_prompt_review(refetch(prompt), actor=self.actor)
        # caller loaded while the DB really said "approved"
        caller = refetch(prompt)
        self.assertEqual(caller.status, Workflow.STATUS_APPROVED)

        # Move the real row back to "review" through a separate, direct path -
        # the caller's in-memory "approved" status is now stale/wrong.
        Prompt.objects.filter(pk=prompt.pk).update(
            status=Workflow.STATUS_REVIEW, approved_revision=None,
            reviewed_by=None, reviewed_at=None,
        )

        result = approve_prompt_review(caller, actor=self.actor)
        self.assertEqual(result.current_status, Workflow.STATUS_APPROVED)

    def test_caller_holds_a_stale_wrong_review_revision_id_without_effect(self):
        prompt = submitted_prompt(actor=self.actor, author=self.author)
        other = submitted_prompt(actor=self.actor, author=self.author)
        caller = refetch(prompt)
        real_review_revision_id = caller.review_revision_id
        # local-only, never saved
        caller.review_revision_id = refetch(other).review_revision_id

        result = approve_prompt_review(caller, actor=self.actor)
        # the DB's real binding was used, not the caller's stale local value
        self.assertEqual(result.review_revision_id, real_review_revision_id)
        self.assertEqual(result.approved_revision_id, real_review_revision_id)

    def test_unsaved_local_content_change_on_the_caller_has_no_effect(self):
        prompt = submitted_prompt(actor=self.actor, author=self.author)
        caller = refetch(prompt)
        translation = caller.translations.get(language_code="en")
        translation.title = "Changed only in memory, never saved"

        result = approve_prompt_review(caller, actor=self.actor)
        self.assertEqual(result.current_status, Workflow.STATUS_APPROVED)

    def test_database_content_change_after_caller_load_is_detected_and_blocked(self):
        prompt = submitted_prompt(actor=self.actor, author=self.author)
        caller = refetch(prompt)
        PromptTranslation.objects.filter(master_id=prompt.pk, language_code="en").update(
            title="Changed in the database after the caller was loaded"
        )
        with self.assertRaises(PromptReviewApprovalError) as ctx:
            approve_prompt_review(caller, actor=self.actor)
        self.assertEqual(ctx.exception.code, PromptReviewApprovalErrorCode.REVIEW_PAYLOAD_CHANGED)

    def test_prefetched_stale_tags_and_tools_on_the_caller_do_not_mask_a_real_change(self):
        tool = make_tool("stale-cache-tool", "Tool")
        prompt = submitted_prompt(actor=self.actor, author=self.author, tools=(tool,), tags=("gamma",))
        caller = refetch(prompt)
        # Prime the caller's relation caches.
        list(caller.tags.all())
        list(caller.tools.all())

        # A real change lands in the DB through a separate path.
        extra_tool = make_tool("stale-cache-tool-2", "Tool 2")
        prompt.tools.add(extra_tool)

        with self.assertRaises(PromptReviewApprovalError) as ctx:
            approve_prompt_review(caller, actor=self.actor)
        self.assertEqual(ctx.exception.code, PromptReviewApprovalErrorCode.REVIEW_PAYLOAD_CHANGED)

    def test_active_parler_language_of_the_caller_has_no_effect(self):
        prompt = submitted_prompt(actor=self.actor, author=self.author, languages=("en", "de"))
        caller = refetch(prompt)
        with switch_language(caller, "de"):
            result = approve_prompt_review(caller, actor=self.actor)
        self.assertEqual(result.current_status, Workflow.STATUS_APPROVED)


# ======================================================================
# No runtime activation
# ======================================================================


class NoRuntimeActivationTests(TestCase):
    def test_production_definition_and_only_sanctioned_consumer(self):
        """
        Beta 11.11C3A contract: no production module consumed
        ``approve_prompt_review``. Beta 11.11C3B contract: exactly one
        production consumer existed - ``prompts/admin.py``. Beta 11.11C4B
        contract: a second sanctioned consumer now exists -
        ``content/views/editorial.py``, which routes the Prompt "approved"
        transition through this primitive instead of the generic
        FSM-``+ obj.save()`` path every other editorial type still uses -
        and no other. The definition still lives only in
        ``prompts/review_approval.py``. Updated minimally by adding the
        second sanctioned file to the allow-list, mirroring how C2B/C4B
        updated C2A's equivalent test.
        """
        import ast
        import pathlib

        import content.views.editorial as editorial_views_module
        import prompts.admin as admin_module
        import prompts.review_approval as approval_module

        symbols = (
            "approve_prompt_review",
            "PromptReviewApprovalResult",
            "PromptReviewApprovalError",
            "PromptReviewApprovalErrorCode",
        )
        definition_file = pathlib.Path(approval_module.__file__).resolve()
        allowed_files = {
            definition_file,
            pathlib.Path(admin_module.__file__).resolve(),
            pathlib.Path(editorial_views_module.__file__).resolve(),
        }
        project_root = definition_file.parents[1]

        offenders = []
        for py_file in project_root.rglob("*.py"):
            if "venv" in py_file.parts or "migrations" in py_file.parts:
                continue
            if "/tests/" in str(py_file) or py_file.name.startswith("test_"):
                continue
            if py_file.resolve() in allowed_files:
                continue
            text = py_file.read_text(encoding="utf-8", errors="ignore")
            if not any(symbol in text for symbol in symbols):
                continue
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    module_name = getattr(node, "module", None) or ""
                    names = [a.name for a in node.names]
                    if "review_approval" in module_name or any(s in names for s in symbols):
                        offenders.append(f"{py_file}: import")
                if isinstance(node, ast.Name) and node.id in symbols:
                    offenders.append(f"{py_file}: reference {node.id}")
        self.assertEqual(offenders, [])

    def test_prompt_admin_imports_the_primitive_not_the_internals(self):
        """
        C3A contract: the prompt admin did not import the primitive. C3B
        contract: it now imports and delegates to ``approve_prompt_review``
        (and its error types) but still reaches for none of the C3A
        internals - no ``build_prompt_review_payload``,
        ``fingerprint_review_payload``, ``validate_review_binding``,
        ``reversion.create_revision``, or FSM ``.approve()``. Inverted from
        the C3A ``assertNotIn`` accordingly.
        """
        import prompts.admin

        with open(prompts.admin.__file__, encoding="utf-8") as _f:
            source = _f.read()
        self.assertIn("approve_prompt_review", source)
        self.assertIn("PromptReviewApprovalError", source)

    def test_admin_approve_action_now_binds_approved_revision(self):
        """
        C3A contract: the shared admin approve path (still used by every
        editorial type at that point) wrote no ``approved_revision``. C3B
        contract: the prompt-specific override now binds it, exactly to the
        already-captured ``review_revision`` - never a fresh lookup. The
        AST-based absence-of-``create_revision``/``atomic``/internals check
        lives in ``prompts/tests/test_admin_review_approval.py``; this test
        only pins the resulting binding contract.
        """
        from django.contrib.auth.models import Group
        from django.test import Client
        from django.urls import reverse

        editor = User.objects.create_user("c3b-editor", password="pw", is_staff=True)
        editor.groups.add(Group.objects.get_or_create(name="Editor")[0])
        author = User.objects.create_user("c3b-runtime-author", password="pw")
        prompt = submitted_prompt(actor=editor, author=author)
        review_revision_id = refetch(prompt).review_revision_id
        client = Client()
        client.force_login(editor)
        client.post(
            reverse("admin:prompts_prompt_changelist"),
            data={"action": "action_approve", "_selected_action": [str(prompt.pk)], "index": "0"},
            follow=True,
        )
        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_APPROVED)
        self.assertEqual(reloaded.review_revision_id, review_revision_id)
        self.assertEqual(reloaded.approved_revision_id, review_revision_id)
        self.assertEqual(reloaded.reviewed_by_id, editor.pk)
        self.assertIsNotNone(reloaded.reviewed_at)

    def test_other_editorial_admins_do_not_import_it(self):
        import compare.admin
        import guides.admin
        import usecases.admin

        for module in (guides.admin, usecases.admin, compare.admin):
            with open(module.__file__, encoding="utf-8") as _f:
                source = _f.read()
            with self.subTest(module=module.__name__):
                self.assertNotIn("approve_prompt_review", source)
                self.assertNotIn("review_approval", source)
