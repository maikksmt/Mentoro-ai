"""
Beta 11.11C4G: the two-phase, not-yet-activated Prompt review edit guard.

``capture_prompt_review_edit_baseline()`` / ``invalidate_prompt_review_if_payload_changed()``
must, together: require an already-active ``transaction.atomic()`` block on
both calls; lock the concrete Prompt root fresh via ``SELECT ... FOR UPDATE``
on each phase; build and fingerprint the payload exclusively through the
existing Beta 11.11C1/C4D ``build_prompt_review_payload()``/
``fingerprint_review_payload()``; call the existing Beta 11.11B2B2
``invalidate_editorial_review_state()`` exactly once, and only when the
fingerprint actually changed; leave every non-payload field (author display
name, live_i18n, workflow/binding metadata, timestamps) with zero influence
on the comparison; tolerate an already-active caller-owned reversion
context without opening one of its own; and roll back completely under a
caller-owned atomic block on any later failure. Nothing here is activated by
any production consumer yet - see ``NoRuntimeActivationTests`` below.
"""
import ast
import itertools
import pathlib
from unittest import mock

import reversion
from django.contrib.auth import get_user_model
from django.db import DEFAULT_DB_ALIAS, connection, connections, transaction
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from reversion.models import Revision, Version

from catalog.models import Tool
from core.models.editorial import EditorialWorkflowMixin as Workflow
from core.review_binding import ReviewInvalidationNoOpReason, fingerprint_review_payload
from prompts.models import Prompt
from prompts.review_approval import approve_prompt_review
from prompts.review_edit_guard import (
    PromptReviewEditBaseline,
    PromptReviewEditGuardError,
    PromptReviewEditGuardErrorCode,
    capture_prompt_review_edit_baseline,
    invalidate_prompt_review_if_payload_changed,
)
from prompts.review_payload import build_prompt_review_payload
from prompts.review_submission import submit_prompt_for_review

User = get_user_model()

_slug_counter = itertools.count()


class PromptProxy(Prompt):
    """Test-local proxy model, mirroring
    ``core.tests.test_review_binding_primitives.GuideProxy``: a proxy
    instance's concrete model must still resolve to ``Prompt`` via
    ``_meta.concrete_model``, never ``type(obj)``."""

    class Meta:
        proxy = True
        app_label = "prompts"


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def refetch(prompt, model=Prompt):
    """django-fsm's protected ``status`` descriptor rejects the ``setattr``
    ``refresh_from_db()`` performs, so reload through the manager."""
    return model.objects.get(pk=prompt.pk)


def make_tool(name):
    tool = Tool.objects.create(slug=f"guard-tool-{next(_slug_counter)}")
    tool.create_translation("en", name=name)
    return tool


def make_prompt(*, status=Workflow.STATUS_DRAFT, author=None, languages=("en",), tools=(), tags=(), **extra):
    prompt = Prompt.objects.create(status=status, author=author, **extra)
    for language_code in languages:
        prompt.create_translation(
            language_code,
            title=f"Title {language_code}",
            intro="intro",
            body="body",
            outro="outro",
            slug=f"guard-slug-{next(_slug_counter)}",
        )
    if tools:
        prompt.tools.add(*tools)
    if tags:
        prompt.tags.add(*tags)
    return prompt


def submitted_prompt(*, actor, author=None, **kwargs):
    prompt = make_prompt(author=author, **kwargs)
    submit_prompt_for_review(prompt, actor=actor)
    return refetch(prompt)


def approved_prompt(*, actor, author=None, **kwargs):
    prompt = submitted_prompt(actor=actor, author=author, **kwargs)
    approve_prompt_review(refetch(prompt), actor=actor)
    return refetch(prompt)


def current_fingerprint(prompt):
    return fingerprint_review_payload(build_prompt_review_payload(refetch(prompt)))


class GuardTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.actor = User.objects.create_user(
            "guard-actor", password="pw", first_name="Grace", last_name="Hopper"
        )


# ======================================================================
# 1. Baseline capture
# ======================================================================


class BaselineCaptureTests(GuardTestCase):
    def test_capture_returns_the_expected_baseline_fields(self):
        prompt = make_prompt(author=self.actor)
        with transaction.atomic():
            baseline = capture_prompt_review_edit_baseline(prompt)
        self.assertIsInstance(baseline, PromptReviewEditBaseline)
        self.assertEqual(baseline.prompt_id, prompt.pk)
        self.assertEqual(baseline.database_alias, DEFAULT_DB_ALIAS)
        self.assertEqual(baseline.payload_schema, "prompt-review-v2")

    def test_capture_fingerprint_matches_the_real_c1_builder(self):
        tool = make_tool("Baseline Tool")
        prompt = make_prompt(author=self.actor, tools=(tool,), tags=("alpha",))
        with transaction.atomic():
            baseline = capture_prompt_review_edit_baseline(prompt)
        expected = fingerprint_review_payload(build_prompt_review_payload(refetch(prompt)))
        self.assertEqual(baseline.payload_fingerprint, expected)

    def test_capture_does_not_mutate_or_save_the_row(self):
        prompt = make_prompt(author=self.actor)
        before_updated_at = refetch(prompt).updated_at
        with transaction.atomic():
            capture_prompt_review_edit_baseline(prompt)
        after = refetch(prompt)
        self.assertEqual(after.updated_at, before_updated_at)
        self.assertEqual(after.status, Workflow.STATUS_DRAFT)

    def test_capture_creates_no_revision_or_version(self):
        prompt = make_prompt(author=self.actor)
        revisions_before = Revision.objects.count()
        versions_before = Version.objects.count()
        with transaction.atomic():
            capture_prompt_review_edit_baseline(prompt)
        self.assertEqual(Revision.objects.count(), revisions_before)
        self.assertEqual(Version.objects.count(), versions_before)

    def test_capture_never_calls_b2b2(self):
        prompt = submitted_prompt(actor=self.actor)
        with mock.patch(
            "prompts.review_edit_guard.invalidate_editorial_review_state"
        ) as invalidate, transaction.atomic():
            capture_prompt_review_edit_baseline(prompt)
        invalidate.assert_not_called()


# ======================================================================
# 2. Baseline validation / misuse
# ======================================================================


class BaselineValidationTests(GuardTestCase):
    def _baseline_for(self, prompt):
        with transaction.atomic():
            return capture_prompt_review_edit_baseline(prompt)

    def test_non_baseline_object_is_rejected(self):
        prompt = make_prompt(author=self.actor)
        with transaction.atomic(), self.assertRaises(PromptReviewEditGuardError) as ctx:
            invalidate_prompt_review_if_payload_changed(prompt, baseline={"not": "a baseline"})
        self.assertEqual(ctx.exception.code, PromptReviewEditGuardErrorCode.INVALID_BASELINE)

    def test_baseline_from_another_prompt_is_rejected(self):
        prompt_a = make_prompt(author=self.actor)
        prompt_b = make_prompt(author=self.actor)
        baseline_a = self._baseline_for(prompt_a)
        with transaction.atomic(), self.assertRaises(PromptReviewEditGuardError) as ctx:
            invalidate_prompt_review_if_payload_changed(prompt_b, baseline=baseline_a)
        self.assertEqual(ctx.exception.code, PromptReviewEditGuardErrorCode.BASELINE_PROMPT_MISMATCH)

    def test_baseline_from_another_alias_is_rejected(self):
        prompt = make_prompt(author=self.actor)
        baseline = self._baseline_for(prompt)
        forged = PromptReviewEditBaseline(
            prompt_id=baseline.prompt_id,
            database_alias="not-a-real-alias",
            payload_schema=baseline.payload_schema,
            payload_fingerprint=baseline.payload_fingerprint,
        )
        with transaction.atomic(), self.assertRaises(PromptReviewEditGuardError) as ctx:
            invalidate_prompt_review_if_payload_changed(prompt, baseline=forged)
        self.assertEqual(
            ctx.exception.code, PromptReviewEditGuardErrorCode.BASELINE_DATABASE_ALIAS_MISMATCH
        )

    def test_manipulated_schema_is_rejected(self):
        prompt = make_prompt(author=self.actor)
        baseline = self._baseline_for(prompt)
        forged = PromptReviewEditBaseline(
            prompt_id=baseline.prompt_id,
            database_alias=baseline.database_alias,
            payload_schema="prompt-review-v999",
            payload_fingerprint=baseline.payload_fingerprint,
        )
        with transaction.atomic(), self.assertRaises(PromptReviewEditGuardError) as ctx:
            invalidate_prompt_review_if_payload_changed(prompt, baseline=forged)
        self.assertEqual(ctx.exception.code, PromptReviewEditGuardErrorCode.BASELINE_SCHEMA_MISMATCH)

    def test_empty_fingerprint_is_rejected(self):
        prompt = make_prompt(author=self.actor)
        baseline = self._baseline_for(prompt)
        forged = PromptReviewEditBaseline(
            prompt_id=baseline.prompt_id, database_alias=baseline.database_alias,
            payload_schema=baseline.payload_schema, payload_fingerprint="",
        )
        with transaction.atomic(), self.assertRaises(PromptReviewEditGuardError) as ctx:
            invalidate_prompt_review_if_payload_changed(prompt, baseline=forged)
        self.assertEqual(
            ctx.exception.code, PromptReviewEditGuardErrorCode.BASELINE_FINGERPRINT_INVALID
        )

    def test_too_short_fingerprint_is_rejected(self):
        prompt = make_prompt(author=self.actor)
        baseline = self._baseline_for(prompt)
        forged = PromptReviewEditBaseline(
            prompt_id=baseline.prompt_id, database_alias=baseline.database_alias,
            payload_schema=baseline.payload_schema, payload_fingerprint="ab12",
        )
        with transaction.atomic(), self.assertRaises(PromptReviewEditGuardError) as ctx:
            invalidate_prompt_review_if_payload_changed(prompt, baseline=forged)
        self.assertEqual(
            ctx.exception.code, PromptReviewEditGuardErrorCode.BASELINE_FINGERPRINT_INVALID
        )

    def test_too_long_fingerprint_is_rejected(self):
        prompt = make_prompt(author=self.actor)
        baseline = self._baseline_for(prompt)
        forged = PromptReviewEditBaseline(
            prompt_id=baseline.prompt_id, database_alias=baseline.database_alias,
            payload_schema=baseline.payload_schema,
            payload_fingerprint=baseline.payload_fingerprint + "0",
        )
        with transaction.atomic(), self.assertRaises(PromptReviewEditGuardError) as ctx:
            invalidate_prompt_review_if_payload_changed(prompt, baseline=forged)
        self.assertEqual(
            ctx.exception.code, PromptReviewEditGuardErrorCode.BASELINE_FINGERPRINT_INVALID
        )

    def test_uppercase_fingerprint_is_rejected(self):
        prompt = make_prompt(author=self.actor)
        baseline = self._baseline_for(prompt)
        forged = PromptReviewEditBaseline(
            prompt_id=baseline.prompt_id, database_alias=baseline.database_alias,
            payload_schema=baseline.payload_schema,
            payload_fingerprint=baseline.payload_fingerprint.upper(),
        )
        with transaction.atomic(), self.assertRaises(PromptReviewEditGuardError) as ctx:
            invalidate_prompt_review_if_payload_changed(prompt, baseline=forged)
        self.assertEqual(
            ctx.exception.code, PromptReviewEditGuardErrorCode.BASELINE_FINGERPRINT_INVALID
        )

    def test_non_hex_characters_are_rejected(self):
        prompt = make_prompt(author=self.actor)
        baseline = self._baseline_for(prompt)
        forged = PromptReviewEditBaseline(
            prompt_id=baseline.prompt_id, database_alias=baseline.database_alias,
            payload_schema=baseline.payload_schema,
            payload_fingerprint="g" * 64,
        )
        with transaction.atomic(), self.assertRaises(PromptReviewEditGuardError) as ctx:
            invalidate_prompt_review_if_payload_changed(prompt, baseline=forged)
        self.assertEqual(
            ctx.exception.code, PromptReviewEditGuardErrorCode.BASELINE_FINGERPRINT_INVALID
        )

    def test_prompt_deleted_after_capture_is_object_not_found(self):
        prompt = make_prompt(author=self.actor)
        baseline = self._baseline_for(prompt)
        Prompt.objects.filter(pk=prompt.pk).delete()
        with transaction.atomic(), self.assertRaises(PromptReviewEditGuardError) as ctx:
            invalidate_prompt_review_if_payload_changed(prompt, baseline=baseline)
        self.assertEqual(ctx.exception.code, PromptReviewEditGuardErrorCode.OBJECT_NOT_FOUND)

    def test_unsaved_prompt_is_rejected(self):
        with transaction.atomic(), self.assertRaises(PromptReviewEditGuardError) as ctx:
            capture_prompt_review_edit_baseline(Prompt())
        self.assertEqual(ctx.exception.code, PromptReviewEditGuardErrorCode.UNSAVED_OBJECT)

    def test_unsupported_object_is_rejected(self):
        with transaction.atomic(), self.assertRaises(PromptReviewEditGuardError) as ctx:
            capture_prompt_review_edit_baseline(None)
        self.assertEqual(ctx.exception.code, PromptReviewEditGuardErrorCode.UNSUPPORTED_OBJECT)

    def test_no_b2b2_call_or_status_change_on_any_baseline_error(self):
        prompt = submitted_prompt(actor=self.actor)
        before = refetch(prompt)
        forged = PromptReviewEditBaseline(
            prompt_id=prompt.pk, database_alias=DEFAULT_DB_ALIAS,
            payload_schema="prompt-review-v2", payload_fingerprint="0" * 63 + "z",
        )
        with transaction.atomic(), self.assertRaises(PromptReviewEditGuardError):
            invalidate_prompt_review_if_payload_changed(prompt, baseline=forged)
        after = refetch(prompt)
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.review_revision_id, before.review_revision_id)


# ======================================================================
# 3. No payload change -> true no-op
# ======================================================================


class NoPayloadChangeTests(GuardTestCase):
    def test_unchanged_payload_is_a_true_noop(self):
        prompt = submitted_prompt(actor=self.actor)
        with transaction.atomic():
            baseline = capture_prompt_review_edit_baseline(prompt)
            result = invalidate_prompt_review_if_payload_changed(prompt, baseline=baseline)
        self.assertFalse(result.payload_changed)
        self.assertFalse(result.invalidated)
        self.assertIsNone(result.invalidation)
        self.assertIsNone(result.no_op_reason)
        self.assertEqual(result.current_fingerprint, baseline.payload_fingerprint)

        after = refetch(prompt)
        self.assertEqual(after.status, Workflow.STATUS_REVIEW)
        self.assertIsNotNone(after.review_revision_id)

    def test_unchanged_payload_does_not_call_b2b2(self):
        prompt = submitted_prompt(actor=self.actor)
        with mock.patch(
            "prompts.review_edit_guard.invalidate_editorial_review_state"
        ) as invalidate, transaction.atomic():
            baseline = capture_prompt_review_edit_baseline(prompt)
            invalidate_prompt_review_if_payload_changed(prompt, baseline=baseline)
        invalidate.assert_not_called()

    def test_unchanged_payload_does_not_touch_updated_at(self):
        prompt = submitted_prompt(actor=self.actor)
        before_updated_at = refetch(prompt).updated_at
        with transaction.atomic():
            baseline = capture_prompt_review_edit_baseline(prompt)
            invalidate_prompt_review_if_payload_changed(prompt, baseline=baseline)
        self.assertEqual(refetch(prompt).updated_at, before_updated_at)

    def test_result_reports_status_even_without_invalidation(self):
        prompt = approved_prompt(actor=self.actor)
        with transaction.atomic():
            baseline = capture_prompt_review_edit_baseline(prompt)
            result = invalidate_prompt_review_if_payload_changed(prompt, baseline=baseline)
        self.assertEqual(result.previous_status, Workflow.STATUS_APPROVED)
        self.assertEqual(result.current_status, Workflow.STATUS_APPROVED)


# ======================================================================
# 4/5. Payload change: review and approved
# ======================================================================


class PayloadChangeReviewTests(GuardTestCase):
    def _guarded(self, prompt, mutate):
        with transaction.atomic():
            baseline = capture_prompt_review_edit_baseline(prompt)
            mutate(refetch(prompt))
            result = invalidate_prompt_review_if_payload_changed(prompt, baseline=baseline)
        return result

    def test_translation_changed(self):
        prompt = submitted_prompt(actor=self.actor)

        def mutate(p):
            from prompts.models import PromptTranslation

            PromptTranslation.objects.filter(master_id=p.pk, language_code="en").update(
                title="Changed title"
            )

        result = self._guarded(prompt, mutate)
        self.assertTrue(result.payload_changed)
        self.assertTrue(result.invalidated)
        self.assertEqual(result.current_status, Workflow.STATUS_DRAFT)
        reloaded = refetch(prompt)
        self.assertIsNone(reloaded.review_revision_id)
        self.assertEqual(reloaded.review_payload_fingerprint, "")

    def test_translation_added(self):
        prompt = submitted_prompt(actor=self.actor)

        def mutate(p):
            p.create_translation(
                "de", title="DE", intro="i", body="b", outro="o", slug=f"guard-de-{next(_slug_counter)}"
            )

        result = self._guarded(prompt, mutate)
        self.assertTrue(result.payload_changed)
        self.assertTrue(result.invalidated)

    def test_translation_removed(self):
        prompt = submitted_prompt(actor=self.actor, languages=("en", "de"))

        def mutate(p):
            p.delete_translation("de")

        result = self._guarded(prompt, mutate)
        self.assertTrue(result.payload_changed)
        self.assertTrue(result.invalidated)

    def test_tag_added(self):
        prompt = submitted_prompt(actor=self.actor, tags=("alpha",))

        def mutate(p):
            p.tags.add("beta")

        result = self._guarded(prompt, mutate)
        self.assertTrue(result.payload_changed)
        self.assertTrue(result.invalidated)

    def test_tag_removed(self):
        prompt = submitted_prompt(actor=self.actor, tags=("alpha", "beta"))

        def mutate(p):
            p.tags.remove("beta")

        result = self._guarded(prompt, mutate)
        self.assertTrue(result.payload_changed)
        self.assertTrue(result.invalidated)

    def test_tool_added(self):
        tool = make_tool("Guard Add Tool")
        prompt = submitted_prompt(actor=self.actor)

        def mutate(p):
            p.tools.add(tool)

        result = self._guarded(prompt, mutate)
        self.assertTrue(result.payload_changed)
        self.assertTrue(result.invalidated)

    def test_tool_removed(self):
        tool = make_tool("Guard Remove Tool")
        prompt = submitted_prompt(actor=self.actor, tools=(tool,))

        def mutate(p):
            p.tools.remove(tool)

        result = self._guarded(prompt, mutate)
        self.assertTrue(result.payload_changed)
        self.assertTrue(result.invalidated)

    def test_author_reassigned_a_to_b(self):
        author_b = User.objects.create_user("guard-author-b", password="pw")
        prompt = submitted_prompt(actor=self.actor, author=self.actor)

        def mutate(p):
            Prompt.objects.filter(pk=p.pk).update(author=author_b)

        result = self._guarded(prompt, mutate)
        self.assertTrue(result.payload_changed)
        self.assertTrue(result.invalidated)

    def test_author_reassigned_a_to_none(self):
        prompt = submitted_prompt(actor=self.actor, author=self.actor)

        def mutate(p):
            Prompt.objects.filter(pk=p.pk).update(author=None)

        result = self._guarded(prompt, mutate)
        self.assertTrue(result.payload_changed)
        self.assertTrue(result.invalidated)

    def test_author_reassigned_none_to_a(self):
        prompt = submitted_prompt(actor=self.actor, author=None)

        def mutate(p):
            Prompt.objects.filter(pk=p.pk).update(author=self.actor)

        result = self._guarded(prompt, mutate)
        self.assertTrue(result.payload_changed)
        self.assertTrue(result.invalidated)

    def test_target_status_is_draft_without_a_live_snapshot(self):
        prompt = submitted_prompt(actor=self.actor)
        self.assertEqual(refetch(prompt).live_i18n, {})

        def mutate(p):
            from prompts.models import PromptTranslation

            PromptTranslation.objects.filter(master_id=p.pk).update(title="Changed")

        result = self._guarded(prompt, mutate)
        self.assertEqual(result.current_status, Workflow.STATUS_DRAFT)
        self.assertFalse(result.had_live_snapshot)

    def test_target_status_is_draft_even_with_a_live_snapshot(self):
        prompt = submitted_prompt(actor=self.actor)
        Prompt.objects.filter(pk=prompt.pk).update(live_i18n={"en": {"title": "Live"}})

        def mutate(p):
            from prompts.models import PromptTranslation

            PromptTranslation.objects.filter(master_id=p.pk).update(title="Changed")

        result = self._guarded(prompt, mutate)
        # Beta 11.11D1: an automatic payload invalidation always targets
        # draft. had_live_snapshot stays truthful - it is what keeps the
        # prompt publicly visible - but no longer decides the status.
        self.assertEqual(result.current_status, Workflow.STATUS_DRAFT)
        self.assertTrue(result.had_live_snapshot)


class PayloadChangeApprovedTests(GuardTestCase):
    def test_translation_change_invalidates_an_approved_prompt(self):
        prompt = approved_prompt(actor=self.actor)
        with transaction.atomic():
            baseline = capture_prompt_review_edit_baseline(prompt)
            from prompts.models import PromptTranslation

            PromptTranslation.objects.filter(master_id=prompt.pk).update(title="Changed")
            result = invalidate_prompt_review_if_payload_changed(prompt, baseline=baseline)

        self.assertTrue(result.payload_changed)
        self.assertTrue(result.invalidated)
        self.assertEqual(result.current_status, Workflow.STATUS_DRAFT)
        reloaded = refetch(prompt)
        self.assertIsNone(reloaded.approved_revision_id)
        self.assertIsNone(reloaded.review_revision_id)

    def test_target_status_is_draft_with_a_live_snapshot_when_approved(self):
        prompt = approved_prompt(actor=self.actor)
        Prompt.objects.filter(pk=prompt.pk).update(live_i18n={"en": {"title": "Live"}})
        with transaction.atomic():
            baseline = capture_prompt_review_edit_baseline(prompt)
            prompt.tags.add("guard-approved-tag")
            result = invalidate_prompt_review_if_payload_changed(prompt, baseline=baseline)
        # Beta 11.11D1: always draft, live snapshot or not.
        self.assertEqual(result.current_status, Workflow.STATUS_DRAFT)


# ======================================================================
# 6. Non-invalidatable statuses
# ======================================================================


class NonInvalidatableStatusTests(GuardTestCase):
    def _assert_noop_status(self, prompt):
        with transaction.atomic():
            baseline = capture_prompt_review_edit_baseline(prompt)
            from prompts.models import PromptTranslation

            PromptTranslation.objects.filter(master_id=prompt.pk).update(title="Changed for noop")
            result = invalidate_prompt_review_if_payload_changed(prompt, baseline=baseline)

        self.assertTrue(result.payload_changed)
        self.assertFalse(result.invalidated)
        self.assertEqual(result.no_op_reason, ReviewInvalidationNoOpReason.STATUS_NOT_REVIEWABLE)
        self.assertIsNotNone(result.invalidation)
        self.assertEqual(result.previous_status, result.current_status)
        return result

    def test_draft_status_is_a_b2b2_noop(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.actor)
        result = self._assert_noop_status(prompt)
        self.assertEqual(result.current_status, Workflow.STATUS_DRAFT)

    def test_rework_status_is_a_b2b2_noop(self):
        prompt = make_prompt(status=Workflow.STATUS_REWORK, author=self.actor)
        result = self._assert_noop_status(prompt)
        self.assertEqual(result.current_status, Workflow.STATUS_REWORK)

    def test_published_status_is_a_b2b2_noop(self):
        prompt = make_prompt(status=Workflow.STATUS_PUBLISHED, author=self.actor)
        result = self._assert_noop_status(prompt)
        self.assertEqual(result.current_status, Workflow.STATUS_PUBLISHED)

    def test_archived_status_is_a_b2b2_noop(self):
        prompt = make_prompt(status=Workflow.STATUS_ARCHIVED, author=self.actor)
        result = self._assert_noop_status(prompt)
        self.assertEqual(result.current_status, Workflow.STATUS_ARCHIVED)


# ======================================================================
# 7. Author display never invalidates
# ======================================================================


class AuthorDisplayNonChangeTests(GuardTestCase):
    def _assert_noop_change(self, prompt, mutate):
        with transaction.atomic():
            baseline = capture_prompt_review_edit_baseline(prompt)
            mutate()
            result = invalidate_prompt_review_if_payload_changed(prompt, baseline=baseline)
        self.assertFalse(result.payload_changed)
        self.assertFalse(result.invalidated)
        return result

    def test_first_name_change_does_not_invalidate(self):
        author = User.objects.create_user("guard-disp-first", password="pw", first_name="Old")
        prompt = submitted_prompt(actor=self.actor, author=author)
        self._assert_noop_change(
            prompt, lambda: User.objects.filter(pk=author.pk).update(first_name="New")
        )

    def test_last_name_change_does_not_invalidate(self):
        author = User.objects.create_user("guard-disp-last", password="pw", last_name="Old")
        prompt = submitted_prompt(actor=self.actor, author=author)
        self._assert_noop_change(
            prompt, lambda: User.objects.filter(pk=author.pk).update(last_name="New")
        )

    def test_username_change_does_not_invalidate(self):
        author = User.objects.create_user("guard-disp-username-old", password="pw")
        prompt = submitted_prompt(actor=self.actor, author=author)
        self._assert_noop_change(
            prompt, lambda: User.objects.filter(pk=author.pk).update(username="guard-disp-username-new")
        )

    def test_live_author_change_does_not_invalidate(self):
        prompt = submitted_prompt(actor=self.actor, author=self.actor)
        self._assert_noop_change(
            prompt,
            lambda: Prompt.objects.filter(pk=prompt.pk).update(
                live_author={"schema": "prompt-author-v1", "display_name": "Anything"}
            ),
        )


# ======================================================================
# 8. live_i18n alone is not part of the payload
# ======================================================================


class LiveSnapshotTests(GuardTestCase):
    def test_live_i18n_alone_does_not_invalidate(self):
        prompt = submitted_prompt(actor=self.actor)
        with transaction.atomic():
            baseline = capture_prompt_review_edit_baseline(prompt)
            Prompt.objects.filter(pk=prompt.pk).update(live_i18n={"en": {"title": "Something else"}})
            result = invalidate_prompt_review_if_payload_changed(prompt, baseline=baseline)
        self.assertFalse(result.payload_changed)
        self.assertFalse(result.invalidated)
        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_REVIEW)
        self.assertIsNotNone(reloaded.review_revision_id)


# ======================================================================
# 9. Alias and proxy support
# ======================================================================


class AliasAndProxyTests(GuardTestCase):
    def test_explicit_matching_alias_is_accepted(self):
        prompt = make_prompt(author=self.actor)
        with transaction.atomic(using=DEFAULT_DB_ALIAS):
            baseline = capture_prompt_review_edit_baseline(prompt, using=DEFAULT_DB_ALIAS)
        self.assertEqual(baseline.database_alias, DEFAULT_DB_ALIAS)

    def test_unknown_alias_name_is_rejected(self):
        prompt = make_prompt(author=self.actor)
        with transaction.atomic(), self.assertRaises(PromptReviewEditGuardError) as ctx:
            capture_prompt_review_edit_baseline(prompt, using="not-a-real-alias")
        self.assertEqual(ctx.exception.code, PromptReviewEditGuardErrorCode.INVALID_DATABASE_ALIAS)

    def test_alias_contradicting_the_objects_own_alias_is_a_mismatch(self):
        prompt = make_prompt(author=self.actor)
        prompt._state.db = "not-default"
        with transaction.atomic(), self.assertRaises(PromptReviewEditGuardError) as ctx:
            capture_prompt_review_edit_baseline(prompt, using=DEFAULT_DB_ALIAS)
        self.assertEqual(ctx.exception.code, PromptReviewEditGuardErrorCode.DATABASE_ALIAS_MISMATCH)

    def test_invalid_using_type_raises_type_error(self):
        prompt = make_prompt(author=self.actor)
        with transaction.atomic(), self.assertRaises(TypeError):
            capture_prompt_review_edit_baseline(prompt, using=123)

    def test_prompt_proxy_is_accepted_and_resolves_to_the_concrete_prompt(self):
        prompt = make_prompt(author=self.actor)
        proxy = PromptProxy.objects.get(pk=prompt.pk)
        with transaction.atomic():
            baseline = capture_prompt_review_edit_baseline(proxy)
        self.assertEqual(baseline.prompt_id, prompt.pk)

        with transaction.atomic():
            result = invalidate_prompt_review_if_payload_changed(proxy, baseline=baseline)
        self.assertFalse(result.payload_changed)

    def test_unrelated_model_is_rejected(self):
        tool = Tool.objects.create(slug="guard-unrelated-tool")
        with transaction.atomic(), self.assertRaises(PromptReviewEditGuardError) as ctx:
            capture_prompt_review_edit_baseline(tool)
        self.assertEqual(ctx.exception.code, PromptReviewEditGuardErrorCode.UNSUPPORTED_OBJECT)


# ======================================================================
# 10. Atomic-context requirement
# ======================================================================


class AtomicContextTests(TransactionTestCase):
    """``TestCase`` wraps every test method in its own outer transaction for
    isolation, so ``in_atomic_block`` would always be ``True`` there -
    ``TransactionTestCase`` is required to genuinely observe "outside any
    atomic block"."""

    def setUp(self):
        self.actor = User.objects.create_user(
            "guard-atomic-actor", password="pw", first_name="Grace", last_name="Hopper"
        )

    def test_capture_outside_atomic_fails_closed(self):
        prompt = make_prompt(author=self.actor)
        self.assertFalse(connections[DEFAULT_DB_ALIAS].in_atomic_block)
        with self.assertRaises(PromptReviewEditGuardError) as ctx:
            capture_prompt_review_edit_baseline(prompt)
        self.assertEqual(ctx.exception.code, PromptReviewEditGuardErrorCode.ATOMIC_CONTEXT_REQUIRED)

    def test_capture_outside_atomic_performs_no_query(self):
        prompt = make_prompt(author=self.actor)
        with CaptureQueriesContext(connection) as ctx, self.assertRaises(PromptReviewEditGuardError):
            capture_prompt_review_edit_baseline(prompt)
        self.assertEqual(len(ctx.captured_queries), 0)

    def test_compare_outside_atomic_fails_closed(self):
        prompt = make_prompt(author=self.actor)
        with transaction.atomic():
            baseline = capture_prompt_review_edit_baseline(prompt)
        with self.assertRaises(PromptReviewEditGuardError) as ctx:
            invalidate_prompt_review_if_payload_changed(prompt, baseline=baseline)
        self.assertEqual(ctx.exception.code, PromptReviewEditGuardErrorCode.ATOMIC_CONTEXT_REQUIRED)

    def test_compare_outside_atomic_performs_no_query(self):
        prompt = make_prompt(author=self.actor)
        with transaction.atomic():
            baseline = capture_prompt_review_edit_baseline(prompt)
        with CaptureQueriesContext(connection) as ctx, self.assertRaises(PromptReviewEditGuardError):
            invalidate_prompt_review_if_payload_changed(prompt, baseline=baseline)
        self.assertEqual(len(ctx.captured_queries), 0)


# ======================================================================
# 11. Reversion context
# ======================================================================


class ReversionContextTests(GuardTestCase):
    def test_guard_runs_inside_an_active_caller_owned_reversion_context(self):
        prompt = submitted_prompt(actor=self.actor)
        revisions_before = Revision.objects.count()

        with transaction.atomic(), reversion.create_revision():
            reversion.set_user(self.actor)
            reversion.set_comment("caller-owned-edit")
            baseline = capture_prompt_review_edit_baseline(prompt)
            from prompts.models import PromptTranslation

            PromptTranslation.objects.filter(master_id=prompt.pk).update(title="Edited under caller revision")
            result = invalidate_prompt_review_if_payload_changed(prompt, baseline=baseline)

        self.assertTrue(result.invalidated)
        self.assertEqual(Revision.objects.count(), revisions_before + 1)

        new_revision = Revision.objects.latest("pk")
        self.assertEqual(new_revision.comment, "caller-owned-edit")
        self.assertEqual(new_revision.user_id, self.actor.pk)

        prompt_versions = new_revision.version_set.filter(
            content_type__app_label="prompts", content_type__model="prompt", object_id=str(prompt.pk)
        )
        self.assertEqual(prompt_versions.count(), 1)
        import json

        fields = json.loads(prompt_versions.get().serialized_data)[0]["fields"]
        self.assertEqual(fields["status"], Workflow.STATUS_DRAFT)
        self.assertIsNone(fields["review_revision"])

    def test_guard_does_not_open_a_second_revision(self):
        prompt = submitted_prompt(actor=self.actor)
        with transaction.atomic(), reversion.create_revision():
            baseline = capture_prompt_review_edit_baseline(prompt)
            prompt.tags.add("guard-revision-tag")
            revisions_before = Revision.objects.count()
            invalidate_prompt_review_if_payload_changed(prompt, baseline=baseline)
            self.assertEqual(Revision.objects.count(), revisions_before)


# ======================================================================
# 12. Rollback
# ======================================================================


class RollbackTests(TransactionTestCase):
    def setUp(self):
        self.actor = User.objects.create_user(
            "guard-rollback-actor", password="pw", first_name="Grace", last_name="Hopper"
        )

    def test_failure_after_mutation_rolls_back_completely(self):
        prompt = submitted_prompt(actor=self.actor)
        before = refetch(prompt)
        revisions_before = Revision.objects.count()

        with self.assertRaises(RuntimeError), transaction.atomic():
            baseline = capture_prompt_review_edit_baseline(prompt)
            from prompts.models import PromptTranslation

            PromptTranslation.objects.filter(master_id=prompt.pk).update(title="Will be rolled back")
            with mock.patch(
                "prompts.review_edit_guard.invalidate_editorial_review_state",
                side_effect=RuntimeError("boom"),
            ):
                invalidate_prompt_review_if_payload_changed(prompt, baseline=baseline)

        after = refetch(prompt)
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.review_revision_id, before.review_revision_id)
        self.assertEqual(after.review_payload_fingerprint, before.review_payload_fingerprint)
        self.assertEqual(
            after.translations.get(language_code="en").title,
            before.translations.get(language_code="en").title,
        )
        self.assertEqual(Revision.objects.count(), revisions_before)

    def test_baseline_is_read_only_even_when_the_transaction_rolls_back(self):
        prompt = submitted_prompt(actor=self.actor)
        before_fp = refetch(prompt).review_payload_fingerprint

        with self.assertRaises(RuntimeError), transaction.atomic():
            capture_prompt_review_edit_baseline(prompt)
            raise RuntimeError("boom, before any mutation")

        after = refetch(prompt)
        self.assertEqual(after.review_payload_fingerprint, before_fp)


# ======================================================================
# 13. Stale caller
# ======================================================================


class StaleCallerTests(GuardTestCase):
    def test_capture_ignores_unsaved_local_field_changes(self):
        prompt = make_prompt(author=self.actor)
        prompt.set_current_language("en")
        prompt.title = "Unsaved Local Title"
        with transaction.atomic():
            baseline = capture_prompt_review_edit_baseline(prompt)
        expected = fingerprint_review_payload(build_prompt_review_payload(refetch(prompt)))
        self.assertEqual(baseline.payload_fingerprint, expected)

    def test_compare_sees_a_change_made_through_a_second_instance(self):
        prompt = submitted_prompt(actor=self.actor)
        stale = refetch(prompt)

        with transaction.atomic():
            baseline = capture_prompt_review_edit_baseline(prompt)
            fresh = Prompt.objects.get(pk=prompt.pk)
            from prompts.models import PromptTranslation

            PromptTranslation.objects.filter(master_id=fresh.pk).update(title="Changed via second instance")
            result = invalidate_prompt_review_if_payload_changed(stale, baseline=baseline)

        self.assertTrue(result.payload_changed)
        self.assertTrue(result.invalidated)


# ======================================================================
# 14. Query and lock contract
# ======================================================================


def query_count_for(ctx, model):
    quoted = f'"{model._meta.db_table}"'
    return sum(1 for q in ctx.captured_queries if quoted in q["sql"])


class QueryAndLockContractTests(GuardTestCase):
    def test_capture_issues_a_for_update_lock_on_the_root(self):
        prompt = make_prompt(author=self.actor)
        with CaptureQueriesContext(connection) as ctx, transaction.atomic():
            capture_prompt_review_edit_baseline(prompt)
        lock_queries = [
            q for q in ctx.captured_queries
            if '"prompts_prompt"' in q["sql"] and "FOR UPDATE" in q["sql"].upper()
        ]
        self.assertGreaterEqual(len(lock_queries), 1)

    def test_capture_issues_no_update_query(self):
        prompt = make_prompt(author=self.actor)
        with CaptureQueriesContext(connection) as ctx, transaction.atomic():
            capture_prompt_review_edit_baseline(prompt)
        updates = [q for q in ctx.captured_queries if q["sql"].strip().upper().startswith("UPDATE")]
        self.assertEqual(updates, [])

    def test_unchanged_payload_issues_no_update_query(self):
        prompt = submitted_prompt(actor=self.actor)
        with transaction.atomic():
            baseline = capture_prompt_review_edit_baseline(prompt)
        with CaptureQueriesContext(connection) as ctx, transaction.atomic():
            invalidate_prompt_review_if_payload_changed(prompt, baseline=baseline)
        updates = [q for q in ctx.captured_queries if q["sql"].strip().upper().startswith("UPDATE")]
        self.assertEqual(updates, [])

    def test_changed_payload_issues_exactly_the_b2b2_root_update(self):
        prompt = submitted_prompt(actor=self.actor)
        with transaction.atomic():
            baseline = capture_prompt_review_edit_baseline(prompt)
            from prompts.models import PromptTranslation

            PromptTranslation.objects.filter(master_id=prompt.pk).update(title="Trigger update")
            with CaptureQueriesContext(connection) as ctx:
                invalidate_prompt_review_if_payload_changed(prompt, baseline=baseline)
        updates = [
            q for q in ctx.captured_queries
            if q["sql"].strip().upper().startswith("UPDATE") and '"prompts_prompt"' in q["sql"]
        ]
        self.assertEqual(len(updates), 1)

    def test_no_auth_user_query_for_a_prompt_without_an_author(self):
        prompt = submitted_prompt(actor=self.actor, author=None)
        with CaptureQueriesContext(connection) as ctx, transaction.atomic():
            baseline = capture_prompt_review_edit_baseline(prompt)
            invalidate_prompt_review_if_payload_changed(prompt, baseline=baseline)
        self.assertEqual(query_count_for(ctx, User), 0)


# ======================================================================
# 15. No runtime activation
# ======================================================================


class NoRuntimeActivationTests(TestCase):
    """
    Beta 11.11C4G shipped the guard with zero production consumers. Beta
    11.11C4H activates exactly one: ``prompts/admin.py``'s ``PromptAdmin``
    changeform. This module's own contract - not a new one - is what changed:
    "no consumers" became "exactly one, precisely named" consumer. Every
    other production module (content/views/editorial.py, models, signals,
    other admin modules, translation-delete) remains forbidden, unchanged.
    """

    def test_admin_py_is_the_sole_production_consumer(self):
        import ast
        import pathlib

        import prompts.admin as admin_module
        import prompts.review_edit_guard as guard_module

        symbols = (
            "capture_prompt_review_edit_baseline",
            "invalidate_prompt_review_if_payload_changed",
        )
        allowed_files = {
            pathlib.Path(guard_module.__file__).resolve(),
            pathlib.Path(admin_module.__file__).resolve(),
        }

        project_root = pathlib.Path(guard_module.__file__).resolve().parents[1]
        offenders = []
        for py_file in project_root.rglob("*.py"):
            if "venv" in py_file.parts or "migrations" in py_file.parts:
                continue
            if "/tests/" in str(py_file) or py_file.name.startswith("test_"):
                continue
            resolved = py_file.resolve()
            if resolved in allowed_files:
                continue
            text = py_file.read_text(encoding="utf-8", errors="ignore")
            if not any(symbol in text for symbol in symbols):
                continue
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    module_name = getattr(node, "module", None) or ""
                    names = [a.name for a in node.names]
                    if "review_edit_guard" in module_name or any(s in names for s in symbols):
                        offenders.append(f"import in {py_file}")
                if isinstance(node, ast.Name) and node.id in symbols:
                    offenders.append(f"reference in {py_file}")

        self.assertEqual(offenders, [])

    def test_admin_module_does_import_the_guard(self):
        """The one sanctioned consumer, precisely - not a broad assertion
        that "some" activation exists, but that this exact module does."""
        import prompts.admin as admin_module

        with open(admin_module.__file__, encoding="utf-8") as _f:
            source = _f.read()
        self.assertIn("review_edit_guard", source)
        self.assertIn("capture_prompt_review_edit_baseline", source)
        self.assertIn("invalidate_prompt_review_if_payload_changed", source)

    def test_editorial_view_module_never_imports_the_guard(self):
        import content.views.editorial as editorial_module

        with open(editorial_module.__file__, encoding="utf-8") as _f:
            source = _f.read()
        self.assertNotIn("review_edit_guard", source)

    def test_translation_delete_view_never_imports_the_guard(self):
        """Beta 11.11C4H integrates only the normal changeform save path -
        the separate Parler translation-delete URL is explicitly out of
        scope and must stay that way."""
        import parler.admin as parler_admin_module

        with open(parler_admin_module.__file__, encoding="utf-8") as _f:
            source = _f.read()
        self.assertNotIn("review_edit_guard", source)

    def test_other_admin_modules_never_import_the_guard(self):
        import compare.admin
        import guides.admin
        import usecases.admin

        for module in (guides.admin, usecases.admin, compare.admin):
            with open(module.__file__, encoding="utf-8") as _f:
                source = _f.read()
            with self.subTest(module=module.__name__):
                self.assertNotIn("review_edit_guard", source)
                self.assertNotIn("capture_prompt_review_edit_baseline", source)
                self.assertNotIn("invalidate_prompt_review_if_payload_changed", source)


# ======================================================================
# 16. Static safety of the production module
# ======================================================================


def _dotted_call_name(node):
    """Reconstructs e.g. ``"transaction.atomic"`` from a Call node's
    ``func``, or ``None`` if it is not a simple dotted attribute/name chain -
    AST-based, so it never matches a docstring mention of the same text the
    way a plain substring search would."""
    func = node.func
    parts = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        parts.append(func.id)
    else:
        return None
    return ".".join(reversed(parts))


class StaticSafetyTests(TestCase):
    """AST-based, not substring-based: ``prompts/review_edit_guard.py``'s own
    module and function docstrings legitimately *describe* ``.save()``,
    ``transaction.atomic()`` and ``reversion.create_revision()`` in prose (to
    explain what this module deliberately does NOT do) - only an actual
    ``ast.Call`` in real code counts as a violation."""

    def test_no_direct_content_or_workflow_mutation(self):
        source = pathlib.Path("prompts/review_edit_guard.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden_attrs = {
            "save", "update", "add", "remove", "set", "clear",
            "create_revision", "set_user", "set_comment", "on_commit",
        }
        offenders = [
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in forbidden_attrs
        ]
        self.assertEqual(offenders, [])

    def test_no_broad_exception_handling(self):
        for path in ("prompts/review_edit_guard.py", "prompts/tests/test_review_edit_guard.py"):
            source = pathlib.Path(path).read_text(encoding="utf-8")
            tree = ast.parse(source)
            offenders = []
            for node in ast.walk(tree):
                if not isinstance(node, ast.ExceptHandler) or node.type is None:
                    continue
                names = [node.type] if isinstance(node.type, ast.Name) else list(
                    getattr(node.type, "elts", [])
                )
                for name_node in names:
                    if isinstance(name_node, ast.Name) and name_node.id in (
                        "Exception", "DatabaseError", "IntegrityError",
                    ):
                        offenders.append(name_node.id)
            with self.subTest(path=path):
                self.assertEqual(offenders, [])

    def test_no_test_shortcuts_in_the_new_test_module(self):
        """AST-based decorator check: this test file's own
        ``test_no_broad_exception_handling`` legitimately mentions
        ``"expectedFailure"`` as a plain string literal (a search term for
        checking *other* files) - that is never a decorator node, so it
        cannot self-match here the way a substring search would."""
        source = pathlib.Path("prompts/tests/test_review_edit_guard.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden_decorators = {"skip", "skipIf", "skipUnless", "expectedFailure"}
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for dec in node.decorator_list:
                target = dec.func if isinstance(dec, ast.Call) else dec
                name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None)
                if name in forbidden_decorators:
                    offenders.append(name)
        self.assertEqual(offenders, [])

    def test_no_test_shortcuts_in_the_production_module(self):
        source = pathlib.Path("prompts/review_edit_guard.py").read_text(encoding="utf-8")
        self.assertNotIn("pragma: no cover", source)
        self.assertNotIn("expectedFailure", source)
        self.assertNotIn("skipIf", source)
        self.assertNotIn("skipUnless", source)

    def test_no_own_transaction_or_reversion_context_is_opened(self):
        source = pathlib.Path("prompts/review_edit_guard.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden_calls = {"transaction.atomic", "reversion.create_revision", "reversion.is_active"}
        offenders = [
            name
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            for name in [_dotted_call_name(node)]
            if name in forbidden_calls
        ]
        self.assertEqual(offenders, [])

    def test_no_payload_or_fingerprint_duplication(self):
        source = pathlib.Path("prompts/review_edit_guard.py").read_text(encoding="utf-8")
        self.assertNotIn("json.dumps", source)
        self.assertNotIn("hashlib", source)
        self.assertNotIn("get_full_name", source)
        self.assertNotIn("display_name", source)
