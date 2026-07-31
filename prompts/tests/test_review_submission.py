"""
Beta 11.11C2A: atomic, per-root prompt review submission.

``submit_prompt_for_review`` is the first primitive that opens a reversion
context, captures the exact revision it produces, and binds it - all under one
outer transaction around a ``SELECT ... FOR UPDATE``-locked root. These tests
hold it to that full contract: input/alias/actor validation, the fresh-row
source of truth, per-root revision isolation, concurrency-safe capture via a
ContextVar-gated per-call receiver, the C1 fingerprint round-trip, and a
complete rollback matrix proving no orphan revision, version or partial binding
survives any failure.
"""
import itertools
import json
import types
from unittest import mock

import reversion
from django.contrib.auth import get_user_model
from django.db import DEFAULT_DB_ALIAS, connection
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from reversion.models import Revision, Version
from reversion.signals import post_revision_commit

from catalog.models import Tool
from core.models.editorial import EditorialWorkflowMixin as Workflow
from core.review_binding import fingerprint_review_payload
from guides.models import Guide
from prompts.models import Prompt, PromptTranslation
from prompts.review_payload import build_prompt_review_payload
from prompts.review_submission import (
    SUBMIT_REVISION_COMMENT,
    PromptReviewSubmissionError,
    PromptReviewSubmissionErrorCode,
    PromptReviewSubmissionResult,
    _active_submission_token,
    submit_prompt_for_review,
)

User = get_user_model()

_slug_counter = itertools.count()


def refetch(prompt):
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


def root_versions(revision):
    return list(revision.version_set.filter(content_type__model="prompt"))


def translation_versions(revision):
    return list(revision.version_set.filter(content_type__model="prompttranslation"))


def version_fields(version):
    return json.loads(version.serialized_data)[0]["fields"]


class SubmissionTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.actor = User.objects.create_user(
            "c2a-actor", password="pw", first_name="Ada", last_name="Lovelace"
        )


# ======================================================================
# Phase 4: result / error contract
# ======================================================================


class ResultAndErrorContractTests(SubmissionTestCase):
    def test_result_is_frozen_and_slotted(self):
        result = PromptReviewSubmissionResult(
            prompt_id=1,
            previous_status="draft",
            current_status="review",
            revision_id=5,
            fingerprint="a" * 64,
            database_alias="default",
        )
        with self.assertRaises(AttributeError):
            result.prompt_id = 2
        self.assertTrue(hasattr(type(result), "__slots__"))

    def test_result_holds_no_model_instances(self):
        prompt = make_prompt()
        result = submit_prompt_for_review(prompt, actor=self.actor)
        self.assertIsInstance(result.prompt_id, int)
        self.assertIsInstance(result.revision_id, int)
        self.assertIsInstance(result.fingerprint, str)
        self.assertIsInstance(result.database_alias, str)
        self.assertIsInstance(result.previous_status, str)
        self.assertIsInstance(result.current_status, str)

    def test_error_codes_are_stable_distinct_strings(self):
        codes = list(PromptReviewSubmissionErrorCode)
        self.assertEqual(len(codes), 13)
        self.assertEqual(len(set(codes)), 13)
        for code in codes:
            self.assertIsInstance(code, str)

    def test_error_carries_its_code(self):
        try:
            submit_prompt_for_review(None)
        except PromptReviewSubmissionError as exc:
            self.assertEqual(exc.code, PromptReviewSubmissionErrorCode.UNSUPPORTED_OBJECT)
            self.assertIsInstance(exc, ValueError)
        else:
            self.fail("expected PromptReviewSubmissionError")


# ======================================================================
# Phase 5: input, alias, actor validation
# ======================================================================


class InputValidationTests(SubmissionTestCase):
    def test_none_is_rejected(self):
        with self.assertRaises(PromptReviewSubmissionError) as ctx:
            submit_prompt_for_review(None)
        self.assertEqual(ctx.exception.code, PromptReviewSubmissionErrorCode.UNSUPPORTED_OBJECT)

    def test_other_editorial_type_is_rejected(self):
        guide = Guide.objects.create()
        with self.assertRaises(PromptReviewSubmissionError) as ctx:
            submit_prompt_for_review(guide)
        self.assertEqual(ctx.exception.code, PromptReviewSubmissionErrorCode.UNSUPPORTED_OBJECT)

    def test_translation_is_rejected(self):
        prompt = make_prompt()
        translation = prompt.translations.get(language_code="en")
        with self.assertRaises(PromptReviewSubmissionError) as ctx:
            submit_prompt_for_review(translation)
        self.assertEqual(ctx.exception.code, PromptReviewSubmissionErrorCode.UNSUPPORTED_OBJECT)

    def test_queryset_is_rejected(self):
        make_prompt()
        with self.assertRaises(PromptReviewSubmissionError) as ctx:
            submit_prompt_for_review(Prompt.objects.all())
        self.assertEqual(ctx.exception.code, PromptReviewSubmissionErrorCode.UNSUPPORTED_OBJECT)

    def test_list_is_rejected(self):
        prompt = make_prompt()
        with self.assertRaises(PromptReviewSubmissionError) as ctx:
            submit_prompt_for_review([prompt])
        self.assertEqual(ctx.exception.code, PromptReviewSubmissionErrorCode.UNSUPPORTED_OBJECT)

    def test_unsaved_prompt_is_rejected(self):
        with self.assertRaises(PromptReviewSubmissionError) as ctx:
            submit_prompt_for_review(Prompt())
        self.assertEqual(ctx.exception.code, PromptReviewSubmissionErrorCode.UNSAVED_OBJECT)

    def test_deleted_prompt_raises_object_not_found(self):
        prompt = make_prompt()
        Prompt.objects.filter(pk=prompt.pk).delete()
        with self.assertRaises(PromptReviewSubmissionError) as ctx:
            submit_prompt_for_review(prompt)
        self.assertEqual(ctx.exception.code, PromptReviewSubmissionErrorCode.OBJECT_NOT_FOUND)

    def test_invalid_using_type_raises_type_error(self):
        prompt = make_prompt()
        with self.assertRaises(TypeError):
            submit_prompt_for_review(prompt, using=123)

    def test_unknown_alias_raises_invalid_database_alias(self):
        prompt = make_prompt()
        with self.assertRaises(PromptReviewSubmissionError) as ctx:
            submit_prompt_for_review(prompt, using="not-a-real-alias")
        self.assertEqual(ctx.exception.code, PromptReviewSubmissionErrorCode.INVALID_DATABASE_ALIAS)

    def test_unknown_alias_is_not_misclassified_as_mismatch(self):
        prompt = make_prompt()
        self.assertEqual(prompt._state.db, DEFAULT_DB_ALIAS)
        with self.assertRaises(PromptReviewSubmissionError) as ctx:
            submit_prompt_for_review(prompt, using="bogus")
        self.assertEqual(ctx.exception.code, PromptReviewSubmissionErrorCode.INVALID_DATABASE_ALIAS)

    def test_explicit_alias_contradicting_object_alias_is_mismatch(self):
        prompt = make_prompt()
        prompt._state.db = "not-default"
        with self.assertRaises(PromptReviewSubmissionError) as ctx:
            submit_prompt_for_review(prompt, using=DEFAULT_DB_ALIAS)
        self.assertEqual(ctx.exception.code, PromptReviewSubmissionErrorCode.DATABASE_ALIAS_MISMATCH)

    def test_invalid_actor_type_is_rejected(self):
        prompt = make_prompt()
        with self.assertRaises(PromptReviewSubmissionError) as ctx:
            submit_prompt_for_review(prompt, actor="not-a-user")
        self.assertEqual(ctx.exception.code, PromptReviewSubmissionErrorCode.INVALID_ACTOR)

    def test_unsaved_actor_is_rejected(self):
        prompt = make_prompt()
        with self.assertRaises(PromptReviewSubmissionError) as ctx:
            submit_prompt_for_review(prompt, actor=User(username="ghost"))
        self.assertEqual(ctx.exception.code, PromptReviewSubmissionErrorCode.INVALID_ACTOR)

    def test_actor_of_wrong_model_is_rejected(self):
        prompt = make_prompt()
        tool = make_tool("actor-wrong-model", "Tool")
        with self.assertRaises(PromptReviewSubmissionError) as ctx:
            submit_prompt_for_review(prompt, actor=tool)
        self.assertEqual(ctx.exception.code, PromptReviewSubmissionErrorCode.INVALID_ACTOR)

    def test_actor_database_alias_mismatch_is_rejected(self):
        prompt = make_prompt()
        actor = User.objects.create_user("actor-alias-mismatch", password="pw")
        actor._state.db = "not-default"
        with self.assertRaises(PromptReviewSubmissionError) as ctx:
            submit_prompt_for_review(prompt, actor=actor)
        self.assertEqual(
            ctx.exception.code, PromptReviewSubmissionErrorCode.ACTOR_DATABASE_ALIAS_MISMATCH
        )

    def test_early_errors_perform_no_mutation_and_no_revision(self):
        prompt = make_prompt()
        revisions_before = Revision.objects.count()
        for kwargs in ({"prompt": None}, {"prompt": Prompt()}):
            with self.assertRaises(PromptReviewSubmissionError):
                submit_prompt_for_review(**kwargs)
        with self.assertRaises(PromptReviewSubmissionError):
            submit_prompt_for_review(prompt, using="bogus")
        self.assertEqual(Revision.objects.count(), revisions_before)
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_DRAFT)


# ======================================================================
# Phase 6: success from draft and rework
# ======================================================================


class SuccessTests(SubmissionTestCase):
    def _assert_submitted(self, prompt, result, *, previous_status, fingerprint):
        self.assertEqual(result.previous_status, previous_status)
        self.assertEqual(result.current_status, Workflow.STATUS_REVIEW)
        self.assertEqual(result.fingerprint, fingerprint)
        self.assertEqual(result.database_alias, DEFAULT_DB_ALIAS)
        self.assertEqual(result.prompt_id, prompt.pk)

        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_REVIEW)
        self.assertEqual(reloaded.review_revision_id, result.revision_id)
        self.assertIsNone(reloaded.approved_revision_id)
        self.assertEqual(reloaded.review_payload_fingerprint, fingerprint)
        self.assertIsNone(reloaded.reviewed_by_id)
        self.assertIsNone(reloaded.reviewed_at)
        self.assertIsNotNone(reloaded.submitted_for_review_at)

    def test_submit_from_draft(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.actor)
        fingerprint = fingerprint_review_payload(build_prompt_review_payload(prompt))
        result = submit_prompt_for_review(prompt, actor=self.actor)
        self._assert_submitted(prompt, result, previous_status=Workflow.STATUS_DRAFT, fingerprint=fingerprint)

    def test_submit_from_rework(self):
        prompt = make_prompt(status=Workflow.STATUS_REWORK, author=self.actor)
        fingerprint = fingerprint_review_payload(build_prompt_review_payload(prompt))
        result = submit_prompt_for_review(prompt, actor=self.actor)
        self._assert_submitted(prompt, result, previous_status=Workflow.STATUS_REWORK, fingerprint=fingerprint)

    def test_submit_from_rework_replaces_old_broken_bindings(self):
        prompt = make_prompt(status=Workflow.STATUS_REWORK, author=self.actor)
        stale_revision = Revision.objects.create(date_created=timezone.now(), comment="stale")
        Prompt.objects.filter(pk=prompt.pk).update(
            review_revision=stale_revision,
            approved_revision=stale_revision,
            review_payload_fingerprint="deadbeef" * 8,
            reviewed_by=self.actor,
            reviewed_at=timezone.now(),
        )
        result = submit_prompt_for_review(refetch(prompt), actor=self.actor)

        reloaded = refetch(prompt)
        self.assertEqual(reloaded.review_revision_id, result.revision_id)
        self.assertNotEqual(reloaded.review_revision_id, stale_revision.pk)
        self.assertIsNone(reloaded.approved_revision_id)
        self.assertIsNone(reloaded.reviewed_by_id)
        self.assertIsNone(reloaded.reviewed_at)
        self.assertNotEqual(reloaded.review_payload_fingerprint, "deadbeef" * 8)

    def test_submit_from_rework_leaves_live_snapshot_untouched(self):
        prompt = make_prompt(status=Workflow.STATUS_REWORK, author=self.actor)
        snapshot = {"en": {"title": "Published", "slug": "pub-slug"}}
        Prompt.objects.filter(pk=prompt.pk).update(
            live_i18n=snapshot, last_published_revision_id=42, is_published=True
        )
        submit_prompt_for_review(refetch(prompt), actor=self.actor)
        reloaded = refetch(prompt)
        self.assertEqual(reloaded.live_i18n, snapshot)
        self.assertEqual(reloaded.last_published_revision_id, 42)
        self.assertTrue(reloaded.is_published)

    def test_review_note_is_preserved(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.actor)
        Prompt.objects.filter(pk=prompt.pk).update(review_note="keep this feedback")
        submit_prompt_for_review(refetch(prompt), actor=self.actor)
        self.assertEqual(refetch(prompt).review_note, "keep this feedback")

    def test_submit_without_actor_succeeds(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT)
        result = submit_prompt_for_review(prompt, actor=None)
        self.assertEqual(result.current_status, Workflow.STATUS_REVIEW)
        revision = Revision.objects.get(pk=result.revision_id)
        self.assertIsNone(revision.user_id)


# ======================================================================
# Phase 7: non-submittable statuses
# ======================================================================


class StatusNotSubmittableTests(SubmissionTestCase):
    def test_review_approved_published_archived_are_rejected(self):
        for status in (
            Workflow.STATUS_REVIEW,
            Workflow.STATUS_APPROVED,
            Workflow.STATUS_PUBLISHED,
            Workflow.STATUS_ARCHIVED,
        ):
            with self.subTest(status=status):
                prompt = make_prompt(status=status)
                revisions_before = Revision.objects.count()
                versions_before = Version.objects.count()
                with self.assertRaises(PromptReviewSubmissionError) as ctx:
                    submit_prompt_for_review(prompt)
                self.assertEqual(
                    ctx.exception.code, PromptReviewSubmissionErrorCode.STATUS_NOT_SUBMITTABLE
                )
                self.assertEqual(refetch(prompt).status, status)
                self.assertEqual(Revision.objects.count(), revisions_before)
                self.assertEqual(Version.objects.count(), versions_before)

    def test_no_payload_or_revision_for_non_submittable_status(self):
        prompt = make_prompt(status=Workflow.STATUS_APPROVED)
        with mock.patch(
            "prompts.review_submission.build_prompt_review_payload"
        ) as payload_mock:
            with self.assertRaises(PromptReviewSubmissionError):
                submit_prompt_for_review(prompt)
        payload_mock.assert_not_called()


# ======================================================================
# Phase 8: C1 fingerprint binding
# ======================================================================


class FingerprintBindingTests(SubmissionTestCase):
    def test_stored_fingerprint_matches_c1(self):
        tool = make_tool("fp-c1-tool", "Tool")
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.actor, tools=(tool,), tags=("alpha",))
        expected = fingerprint_review_payload(build_prompt_review_payload(prompt))
        result = submit_prompt_for_review(prompt, actor=self.actor)
        self.assertEqual(result.fingerprint, expected)
        self.assertEqual(refetch(prompt).review_payload_fingerprint, expected)

    def test_rebuilt_fingerprint_after_submit_matches(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.actor)
        result = submit_prompt_for_review(prompt, actor=self.actor)
        rebuilt = fingerprint_review_payload(build_prompt_review_payload(refetch(prompt)))
        self.assertEqual(rebuilt, result.fingerprint)

    def test_active_caller_language_has_no_effect(self):
        from django.utils import translation as dj_translation

        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.actor, languages=("en", "de"))
        expected = fingerprint_review_payload(build_prompt_review_payload(prompt))
        with dj_translation.override("de"):
            result = submit_prompt_for_review(refetch(prompt), actor=self.actor)
        self.assertEqual(result.fingerprint, expected)

    def test_stale_caller_content_does_not_affect_fingerprint(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.actor)
        db_fingerprint = fingerprint_review_payload(build_prompt_review_payload(prompt))
        # mutate the caller object locally without saving
        prompt.set_current_language("en")
        prompt.title = "Unsaved Local Title"
        result = submit_prompt_for_review(prompt, actor=self.actor)
        self.assertEqual(result.fingerprint, db_fingerprint)

    def test_database_content_change_before_submit_is_reflected(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.actor)
        original = fingerprint_review_payload(build_prompt_review_payload(prompt))
        PromptTranslation.objects.filter(master_id=prompt.pk, language_code="en").update(
            title="Changed In DB"
        )
        result = submit_prompt_for_review(prompt, actor=self.actor)
        self.assertNotEqual(result.fingerprint, original)
        expected = fingerprint_review_payload(build_prompt_review_payload(refetch(prompt)))
        self.assertEqual(result.fingerprint, expected)


# ======================================================================
# Phase 9: revision graph
# ======================================================================


class RevisionGraphTests(SubmissionTestCase):
    def test_graph_contains_root_and_all_translations(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.actor, languages=("en", "de"))
        result = submit_prompt_for_review(prompt, actor=self.actor)
        revision = Revision.objects.get(pk=result.revision_id)

        roots = root_versions(revision)
        self.assertEqual([v.object_id for v in roots], [str(prompt.pk)])

        translation_ids = {v.object_id for v in translation_versions(revision)}
        expected_ids = {
            str(pk)
            for pk in PromptTranslation.objects.filter(master_id=prompt.pk).values_list("pk", flat=True)
        }
        self.assertEqual(translation_ids, expected_ids)
        self.assertEqual(len(expected_ids), 2)

    def test_graph_has_no_tag_or_tool_membership(self):
        tool = make_tool("graph-tool", "Tool")
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.actor, tools=(tool,), tags=("alpha",))
        result = submit_prompt_for_review(prompt, actor=self.actor)
        revision = Revision.objects.get(pk=result.revision_id)
        labels = {f"{v.content_type.app_label}.{v.content_type.model}" for v in revision.version_set.all()}
        self.assertEqual(labels, {"prompts.prompt", "prompts.prompttranslation"})

    def test_root_version_holds_submit_state_but_not_self_reference(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.actor)
        result = submit_prompt_for_review(prompt, actor=self.actor)
        revision = Revision.objects.get(pk=result.revision_id)
        fields = version_fields(root_versions(revision)[0])
        self.assertEqual(fields["status"], Workflow.STATUS_REVIEW)
        self.assertEqual(fields["review_payload_fingerprint"], result.fingerprint)
        self.assertIsNone(fields["approved_revision"])
        self.assertIsNone(fields["reviewed_by"])
        self.assertIsNone(fields["reviewed_at"])
        self.assertIsNotNone(fields["submitted_for_review_at"])
        # review_revision is bound only after capture, so the root version is
        # deliberately not self-referential.
        self.assertIsNone(fields["review_revision"])

    def test_current_row_has_the_review_revision_after_binding(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.actor)
        result = submit_prompt_for_review(prompt, actor=self.actor)
        self.assertEqual(refetch(prompt).review_revision_id, result.revision_id)


# ======================================================================
# Phase 10: per-root isolation
# ======================================================================


class PerRootIsolationTests(SubmissionTestCase):
    def test_two_prompts_get_two_separate_revisions(self):
        other_actor = User.objects.create_user("c2a-actor-b", password="pw")
        prompt_a = make_prompt(status=Workflow.STATUS_DRAFT, author=self.actor, languages=("en",))
        prompt_b = make_prompt(status=Workflow.STATUS_DRAFT, author=other_actor, languages=("en", "de"))

        result_a = submit_prompt_for_review(prompt_a, actor=self.actor)
        result_b = submit_prompt_for_review(prompt_b, actor=other_actor)

        self.assertNotEqual(result_a.revision_id, result_b.revision_id)

        revision_a = Revision.objects.get(pk=result_a.revision_id)
        revision_b = Revision.objects.get(pk=result_b.revision_id)

        self.assertEqual([v.object_id for v in root_versions(revision_a)], [str(prompt_a.pk)])
        self.assertEqual([v.object_id for v in root_versions(revision_b)], [str(prompt_b.pk)])
        self.assertNotIn(str(prompt_b.pk), [v.object_id for v in root_versions(revision_a)])
        self.assertNotIn(str(prompt_a.pk), [v.object_id for v in root_versions(revision_b)])

        # translations separated
        a_translation_masters = {
            version_fields(v)["master"] for v in translation_versions(revision_a)
        }
        b_translation_masters = {
            version_fields(v)["master"] for v in translation_versions(revision_b)
        }
        self.assertEqual(a_translation_masters, {prompt_a.pk})
        self.assertEqual(b_translation_masters, {prompt_b.pk})

        # actor + comment per revision
        self.assertEqual(revision_a.user_id, self.actor.pk)
        self.assertEqual(revision_b.user_id, other_actor.pk)
        self.assertEqual(revision_a.comment, SUBMIT_REVISION_COMMENT)
        self.assertEqual(revision_b.comment, SUBMIT_REVISION_COMMENT)


# ======================================================================
# Phase 11: signal / concurrency isolation
# ======================================================================


#: ``submit_prompt_for_review`` connects one ``post_revision_commit`` receiver
#: per call, under a ``prompts.review_submission:<token>`` dispatch_uid, and
#: must disconnect it again in ``finally``.
#:
#: These tests used to prove that by asserting the whole signal had no
#: listeners at all, which only held while this module was the signal's sole
#: user. Beta 11.13D1B added a permanent, module-level receiver in
#: ``core.editorial_actions`` (it writes the publish marker once a revision
#: commits, and must therefore outlive every individual call). Counting *all*
#: listeners would now measure that unrelated receiver instead of the leak
#: these tests exist to catch, so the assertion is scoped to this module's own
#: call-local dispatch_uids - the exact thing that must never accumulate.
_CALL_LOCAL_DISPATCH_PREFIX = "prompts.review_submission:"


def leaked_call_local_receivers():
    """Dispatch uids of this module's per-call receivers still connected."""
    leaked = []
    # Django's ``Signal.receivers`` entries are ``(lookup_key, receiver)`` on
    # older versions and ``(lookup_key, receiver, is_async)`` on newer ones;
    # only the first element is read here so both shapes work.
    for entry in post_revision_commit.receivers:
        lookup_key = entry[0]
        key = lookup_key[0] if isinstance(lookup_key, tuple) else lookup_key
        if isinstance(key, str) and key.startswith(_CALL_LOCAL_DISPATCH_PREFIX):
            leaked.append(key)
    return leaked


class SignalIsolationTests(SubmissionTestCase):
    def test_successful_call_leaves_no_receiver_and_resets_the_context_var(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT)
        submit_prompt_for_review(prompt)
        self.assertIsNone(_active_submission_token.get())
        self.assertEqual(leaked_call_local_receivers(), [])

    def test_failed_call_leaves_no_receiver_and_resets_the_context_var(self):
        prompt = make_prompt(status=Workflow.STATUS_APPROVED)  # not submittable
        with self.assertRaises(PromptReviewSubmissionError):
            submit_prompt_for_review(prompt)
        self.assertIsNone(_active_submission_token.get())
        self.assertEqual(leaked_call_local_receivers(), [])

    def test_failed_call_inside_reversion_context_leaves_no_receiver(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT)
        with mock.patch.object(Prompt, "save", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                submit_prompt_for_review(prompt)
        self.assertIsNone(_active_submission_token.get())
        self.assertEqual(leaked_call_local_receivers(), [])

    def test_repeated_calls_do_not_accumulate_receivers(self):
        for _ in range(3):
            submit_prompt_for_review(make_prompt(status=Workflow.STATUS_DRAFT))
        self.assertEqual(leaked_call_local_receivers(), [])

    def test_foreign_signal_in_a_different_execution_context_is_ignored(self):
        """
        A foreign post_revision_commit fired *without* this call's token active
        must not be captured. Simulated by sending the signal from inside a
        patched set_comment - but under a different (reset) token - proving the
        receiver's ContextVar gate, not luck, is what isolates it.
        """
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.actor)
        foreign_revision = Revision.objects.create(date_created=timezone.now(), comment="foreign")

        real_set_comment = reversion.set_comment

        def leaky_set_comment(comment):
            real_set_comment(comment)
            # Fire a foreign signal while the active token is temporarily reset
            # to something else - the receiver must ignore it.
            reset_token = _active_submission_token.set("some-other-call-token")
            try:
                post_revision_commit.send(
                    sender=reversion.create_revision,
                    revision=foreign_revision,
                    versions=[types.SimpleNamespace(db=DEFAULT_DB_ALIAS)],
                )
            finally:
                _active_submission_token.reset(reset_token)

        with mock.patch("reversion.set_comment", side_effect=leaky_set_comment):
            result = submit_prompt_for_review(prompt, actor=self.actor)

        # The foreign revision was ignored; the real one was captured.
        self.assertNotEqual(result.revision_id, foreign_revision.pk)
        self.assertEqual(refetch(prompt).review_revision_id, result.revision_id)

    def test_foreign_alias_signal_is_ignored(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.actor)
        foreign_revision = Revision.objects.create(date_created=timezone.now(), comment="foreign-alias")
        real_set_comment = reversion.set_comment

        def leaky_set_comment(comment):
            real_set_comment(comment)
            # same token (active) but a version on a different alias -> ignored
            post_revision_commit.send(
                sender=reversion.create_revision,
                revision=foreign_revision,
                versions=[types.SimpleNamespace(db="some-other-alias")],
            )

        with mock.patch("reversion.set_comment", side_effect=leaky_set_comment):
            result = submit_prompt_for_review(prompt, actor=self.actor)

        self.assertNotEqual(result.revision_id, foreign_revision.pk)


# ======================================================================
# Phase 12: active reversion context
# ======================================================================


class ActiveReversionContextTests(SubmissionTestCase):
    def test_running_inside_an_active_context_is_rejected(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT)
        revisions_before = Revision.objects.count()
        with reversion.create_revision():
            with self.assertRaises(PromptReviewSubmissionError) as ctx:
                submit_prompt_for_review(prompt)
        self.assertEqual(
            ctx.exception.code, PromptReviewSubmissionErrorCode.ACTIVE_REVERSION_CONTEXT
        )
        # The outer context is not corrupted; the prompt is unchanged.
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_DRAFT)
        self.assertIsNone(refetch(prompt).review_revision_id)
        # No inner submission revision for this prompt.
        self.assertFalse(
            Version.objects.filter(
                content_type__model="prompt", object_id=str(prompt.pk)
            ).exists()
        )
        self.assertGreaterEqual(Revision.objects.count(), revisions_before)

    def test_rejection_happens_before_any_query_or_mutation(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT)
        with reversion.create_revision():
            with mock.patch(
                "prompts.review_submission.build_prompt_review_payload"
            ) as payload_mock:
                with self.assertRaises(PromptReviewSubmissionError):
                    submit_prompt_for_review(prompt)
            payload_mock.assert_not_called()


# ======================================================================
# Phase 13: payload consistency
# ======================================================================


class PayloadConsistencyTests(SubmissionTestCase):
    def test_unchanged_state_submits_successfully(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.actor)
        result = submit_prompt_for_review(prompt, actor=self.actor)
        self.assertEqual(result.current_status, Workflow.STATUS_REVIEW)

    def test_payload_change_between_builds_aborts_and_rolls_back(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.actor)
        before = self._snapshot(prompt)
        revisions_before = Revision.objects.count()
        versions_before = Version.objects.count()

        payload_a = build_prompt_review_payload(prompt)
        payload_b = {**payload_a, "translations": []}  # deliberately different

        with mock.patch(
            "prompts.review_submission.build_prompt_review_payload",
            side_effect=[payload_a, payload_b],
        ):
            with self.assertRaises(PromptReviewSubmissionError) as ctx:
                submit_prompt_for_review(prompt, actor=self.actor)
        self.assertEqual(
            ctx.exception.code, PromptReviewSubmissionErrorCode.PAYLOAD_CHANGED_DURING_SUBMISSION
        )
        self._assert_unchanged(prompt, before)
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
            "updated_at": fresh.updated_at,
            "live_i18n": fresh.live_i18n,
        }

    def _assert_unchanged(self, prompt, before):
        self.assertEqual(self._snapshot(prompt), before)


# ======================================================================
# Phase 14: rollback matrix
# ======================================================================


class RollbackMatrixTests(SubmissionTestCase):
    def setUp(self):
        self.tool = make_tool("rollback-tool", "Tool")
        self.prompt = make_prompt(
            status=Workflow.STATUS_REWORK, author=self.actor, languages=("en", "de"),
            tools=(self.tool,), tags=("alpha",),
        )
        # give it an old (to-be-replaced) binding so we can prove it survives failure
        self.stale_revision = Revision.objects.create(date_created=timezone.now(), comment="stale")
        Prompt.objects.filter(pk=self.prompt.pk).update(
            review_revision=self.stale_revision,
            approved_revision=self.stale_revision,
            review_payload_fingerprint="c0ffee00" * 8,
            reviewed_by=self.actor,
            reviewed_at=timezone.now(),
            review_note="old feedback",
        )
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
            "updated_at": fresh.updated_at,
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
            "prompts.review_submission.build_prompt_review_payload",
            side_effect=RuntimeError("builder boom"),
        ):
            with self.assertRaises(RuntimeError):
                submit_prompt_for_review(self.prompt, actor=self.actor)
        self._assert_full_rollback()

    def test_fingerprint_failure(self):
        with mock.patch(
            "prompts.review_submission.fingerprint_review_payload",
            side_effect=RuntimeError("fingerprint boom"),
        ):
            with self.assertRaises(RuntimeError):
                submit_prompt_for_review(self.prompt, actor=self.actor)
        self._assert_full_rollback()

    def test_transition_failure(self):
        with mock.patch.object(Prompt, "move_to_review", side_effect=RuntimeError("fsm boom")):
            with self.assertRaises(RuntimeError):
                submit_prompt_for_review(self.prompt, actor=self.actor)
        self._assert_full_rollback()

    def test_submit_save_failure(self):
        with mock.patch.object(Prompt, "save", side_effect=RuntimeError("save boom")):
            with self.assertRaises(RuntimeError):
                submit_prompt_for_review(self.prompt, actor=self.actor)
        self._assert_full_rollback()

    def test_no_revision_captured(self):
        # Prevent the receiver from ever connecting; reversion still creates the
        # revision, but nothing captures it -> REVISION_NOT_CAPTURED, then the
        # outer transaction rolls the created revision back.
        with mock.patch.object(post_revision_commit, "connect"):
            with self.assertRaises(PromptReviewSubmissionError) as ctx:
                submit_prompt_for_review(self.prompt, actor=self.actor)
        self.assertEqual(ctx.exception.code, PromptReviewSubmissionErrorCode.REVISION_NOT_CAPTURED)
        self._assert_full_rollback()

    def test_multiple_revisions_captured(self):
        foreign_revision = Revision.objects.create(date_created=timezone.now(), comment="extra")
        # The foreign revision is a test artifact created outside the
        # submission's transaction, so it survives the rollback - re-baseline
        # the counts so _assert_full_rollback measures only the submission's net
        # effect.
        self.revisions_before = Revision.objects.count()
        self.versions_before = Version.objects.count()
        real_set_comment = reversion.set_comment

        def double_capture(comment):
            real_set_comment(comment)
            # fire an extra signal matching the active token + alias
            post_revision_commit.send(
                sender=reversion.create_revision,
                revision=foreign_revision,
                versions=[types.SimpleNamespace(db=DEFAULT_DB_ALIAS)],
            )

        with mock.patch("reversion.set_comment", side_effect=double_capture):
            with self.assertRaises(PromptReviewSubmissionError) as ctx:
                submit_prompt_for_review(self.prompt, actor=self.actor)
        self.assertEqual(
            ctx.exception.code, PromptReviewSubmissionErrorCode.MULTIPLE_REVISIONS_CAPTURED
        )
        self._assert_full_rollback()

    def test_root_version_missing(self):
        with mock.patch(
            "prompts.review_submission.revision_contains_object", return_value=False
        ):
            with self.assertRaises(PromptReviewSubmissionError) as ctx:
                submit_prompt_for_review(self.prompt, actor=self.actor)
        self.assertEqual(ctx.exception.code, PromptReviewSubmissionErrorCode.ROOT_VERSION_MISSING)
        self._assert_full_rollback()

    def test_payload_changed_during_submission(self):
        payload_a = build_prompt_review_payload(self.prompt)
        payload_b = {**payload_a, "translations": []}
        with mock.patch(
            "prompts.review_submission.build_prompt_review_payload",
            side_effect=[payload_a, payload_b],
        ):
            with self.assertRaises(PromptReviewSubmissionError) as ctx:
                submit_prompt_for_review(self.prompt, actor=self.actor)
        self.assertEqual(
            ctx.exception.code, PromptReviewSubmissionErrorCode.PAYLOAD_CHANGED_DURING_SUBMISSION
        )
        self._assert_full_rollback()

    def test_binding_save_failure(self):
        original_save = Prompt.save
        calls = {"n": 0}

        def flaky_save(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:  # the binding save
                raise RuntimeError("binding save boom")
            return original_save(self, *args, **kwargs)

        with mock.patch.object(Prompt, "save", flaky_save):
            with self.assertRaises(RuntimeError):
                submit_prompt_for_review(self.prompt, actor=self.actor)
        self._assert_full_rollback()


# ======================================================================
# Phase 15: query and lock contract
# ======================================================================


class QueryAndLockContractTests(TransactionTestCase):
    """
    ``select_for_update()`` requires a real transaction, so these run under
    ``TransactionTestCase`` (the wrapping-transaction of ``TestCase`` would
    otherwise absorb the lock semantics).
    """

    def setUp(self):
        self.actor = User.objects.create_user("c2a-lock-actor", password="pw")

    def test_root_is_locked_with_for_update_on_the_prompt_table(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.actor)
        with CaptureQueriesContext(connection) as ctx:
            submit_prompt_for_review(prompt, actor=self.actor)
        for_update = [
            q for q in ctx.captured_queries
            if "FOR UPDATE" in q["sql"].upper() and '"prompts_prompt"' in q["sql"]
        ]
        self.assertEqual(len(for_update), 1)
        lock_sql = for_update[0]["sql"].upper()
        # The unfiltered _default_manager: a plain pk lookup, never the public
        # PublishedOnlyManager (which would add a status filter and a
        # translations join).
        self.assertNotIn("INNER JOIN", lock_sql)
        self.assertNotIn("= 'PUBLISHED'", lock_sql)
        self.assertNotIn("'PUBLISHED'", lock_sql)

    def test_one_submit_update_and_one_binding_update(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.actor)
        with CaptureQueriesContext(connection) as ctx:
            result = submit_prompt_for_review(prompt, actor=self.actor)
        prompt_updates = [
            q for q in ctx.captured_queries
            if q["sql"].strip().upper().startswith("UPDATE") and '"prompts_prompt"' in q["sql"]
        ]
        # exactly two UPDATEs on the prompt row: the submit save and the binding save
        self.assertEqual(len(prompt_updates), 2)
        # the binding update touches only review_revision_id (no updated_at)
        binding_update = prompt_updates[-1]
        self.assertIn("review_revision_id", binding_update["sql"])
        self.assertNotIn("updated_at", binding_update["sql"])
        self.assertEqual(refetch(prompt).review_revision_id, result.revision_id)

    def test_no_revision_lookup_heuristic_query(self):
        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.actor)
        with CaptureQueriesContext(connection) as ctx:
            submit_prompt_for_review(prompt, actor=self.actor)
        # no ORDER BY on the revision table (would betray a newest-revision heuristic)
        for q in ctx.captured_queries:
            sql = q["sql"].upper()
            if '"REVERSION_REVISION"' in sql and "ORDER BY" in sql:
                self.fail(f"unexpected ordered revision lookup: {q['sql']}")

    def test_binding_save_does_not_change_updated_at(self):
        from django.utils.dateparse import parse_datetime

        prompt = make_prompt(status=Workflow.STATUS_DRAFT, author=self.actor)
        submit_prompt_for_review(prompt, actor=self.actor)
        reloaded = refetch(prompt)
        # The root version was serialized during the submit save, before the
        # binding save. If the binding save had touched updated_at (auto_now),
        # the row's value would be a fresh now() many milliseconds later. They
        # agree to within the serializer's millisecond rounding, proving the
        # binding save left updated_at alone.
        revision = Revision.objects.get(pk=reloaded.review_revision_id)
        serialized_updated = parse_datetime(version_fields(root_versions(revision)[0])["updated_at"])
        delta = abs((reloaded.updated_at - serialized_updated).total_seconds())
        self.assertLess(delta, 0.001)


# ======================================================================
# Phase 16: no runtime activation
# ======================================================================


class NoRuntimeActivationTests(TestCase):
    def test_production_definition_and_only_sanctioned_consumer(self):
        """
        Beta 11.11C2A contract: nothing in production consumed
        ``submit_prompt_for_review``. Beta 11.11C2B contract: exactly one
        production consumer existed - ``prompts/admin.py``. Beta 11.11C4B
        contract: a second sanctioned consumer now exists -
        ``content/views/editorial.py``, which routes the Prompt "review"
        transition through this primitive instead of the generic
        FSM-``+ obj.save()`` path every other editorial type still uses -
        and no other. The definition still lives only in
        ``prompts/review_submission.py``. Updated minimally by adding the
        second sanctioned file to the allow-list.
        """
        import ast
        import pathlib

        import content.views.editorial as editorial_views_module
        import prompts.admin as admin_module
        import prompts.review_submission as submission_module

        symbols = (
            "submit_prompt_for_review",
            "PromptReviewSubmissionResult",
            "PromptReviewSubmissionError",
            "PromptReviewSubmissionErrorCode",
        )
        definition_file = pathlib.Path(submission_module.__file__).resolve()
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
                    if "review_submission" in module_name or any(s in names for s in symbols):
                        offenders.append(f"{py_file}: import")
                if isinstance(node, ast.Name) and node.id in symbols:
                    offenders.append(f"{py_file}: reference {node.id}")
        self.assertEqual(offenders, [])

    def test_prompt_admin_imports_the_primitive_not_the_internals(self):
        """
        C2A contract: the prompt admin did not import the primitive. C2B
        contract: it now imports and delegates to ``submit_prompt_for_review``
        (and its error types) but still reaches for none of the C2A internals -
        no ``create_revision``, no ``move_to_review``, no payload/fingerprint
        builder. Inverted from the C2A ``assertNotIn`` accordingly.
        """
        import prompts.admin

        source = open(prompts.admin.__file__, encoding="utf-8").read()
        self.assertIn("submit_prompt_for_review", source)
        self.assertIn("PromptReviewSubmissionError", source)

    def test_admin_submit_action_opens_no_batch_revision(self):
        """
        C2A contract: the admin still batched every selected object into one
        shared ``reversion.create_revision``. C2B contract: it delegates to the
        per-root primitive and opens no batch revision itself - the AST checks
        in ``prompts/tests/test_admin_review_submission.py`` assert the absence
        of ``create_revision``/``atomic`` calls in the action body; here we just
        assert the delegation is present.
        """
        import prompts.admin

        source = open(prompts.admin.__file__, encoding="utf-8").read()
        self.assertIn("submit_prompt_for_review", source)
