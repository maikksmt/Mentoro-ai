"""
Beta 11.11B2B2: atomic review invalidation.

``invalidate_editorial_review_state()`` is the first primitive in the
``core.review_binding`` module that writes anything. Everything before it
(B2B1/B2B1A) was pure and read-only; this one moves a ``review``/``approved``
row to ``draft`` or ``rework`` on a ``SELECT ... FOR UPDATE``-locked row and
clears its binding metadata - deliberately without ever asking whether the
binding it is discarding was valid in the first place, because "the binding
can no longer be trusted" is exactly the situation this function exists to
handle.

These tests hold it to the full atomic contract: the locked database row (not
a possibly-stale in-memory object) decides the outcome; the change is
idempotent; it participates in an already-open ``reversion.create_revision()``
block exactly like an ordinary ``ModelAdmin`` save and creates no revision at
all when no such block is open; and any failure rolls the whole transaction
back, including any partially-built revision.
"""
import json
from unittest import mock

import reversion
from django.contrib.auth import get_user_model
from django.db import DEFAULT_DB_ALIAS, connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from reversion.models import Revision, Version

from catalog.models import Tool
from compare.models import Comparison, ComparisonToolEntry
from core.models.editorial import EditorialWorkflowMixin as Workflow
from core.review_binding import (
    ReviewInvalidationError,
    ReviewInvalidationErrorCode,
    ReviewInvalidationNoOpReason,
    ReviewInvalidationResult,
    fingerprint_review_payload,
    invalidate_editorial_review_state,
    validate_approved_binding,
    validate_review_binding,
)
from guides.models import Guide, GuideItem, GuideSection
from prompts.models import Prompt
from usecases.models import UseCase

User = get_user_model()

EDITORIAL_TYPES = (Guide, Prompt, UseCase, Comparison)

INVALIDATABLE_STATUSES = (Workflow.STATUS_REVIEW, Workflow.STATUS_APPROVED)
UNTOUCHED_STATUSES = (
    Workflow.STATUS_DRAFT,
    Workflow.STATUS_REWORK,
    Workflow.STATUS_PUBLISHED,
    Workflow.STATUS_ARCHIVED,
)

BINDING_FIELDS = (
    "review_revision_id",
    "approved_revision_id",
    "review_payload_fingerprint",
    "reviewed_by_id",
    "reviewed_at",
    "submitted_for_review_at",
)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def refetch(obj):
    """django-fsm's protected ``status`` descriptor rejects the ``setattr``
    ``refresh_from_db()`` performs, so reload through the manager."""
    return type(obj).objects.get(pk=obj.pk)


def make_revision(comment="test revision"):
    """``Revision.date_created`` has no Python-side default - reversion fills
    it when it opens a revision block - so a directly created row must supply
    one."""
    return Revision.objects.create(date_created=timezone.now(), comment=comment)


def create_with_revision(model, **kwargs):
    """Creates ``model(**kwargs)`` inside a real ``reversion.create_revision()``
    block, producing a genuine root Version."""
    with reversion.create_revision():
        obj = model.objects.create(**kwargs)
    return obj


def bound_object(model, *, status, revision, fingerprint=None, reviewer=None, **kwargs):
    """A row with a full, syntactically valid review+approval binding pointed
    at ``revision`` - used by the damaged-binding matrix as the "healthy"
    baseline that each individual field is then broken away from."""
    obj = model.objects.create(
        status=status,
        review_revision=revision,
        approved_revision=revision,
        review_payload_fingerprint=fingerprint or fingerprint_review_payload({"a": 1}),
        reviewed_by=reviewer,
        reviewed_at=timezone.now(),
        submitted_for_review_at=timezone.now(),
        review_note="reviewer feedback that must survive",
        **kwargs,
    )
    return obj


def concrete_table_queries(ctx, model):
    table = model._meta.db_table
    return [q for q in ctx.captured_queries if table in q["sql"]]


def is_update_sql(sql):
    return sql.strip().upper().startswith("UPDATE")


def is_select_for_update_sql(sql):
    upper = sql.strip().upper()
    return upper.startswith("SELECT") and "FOR UPDATE" in upper


def count_updates(ctx, model):
    return sum(1 for q in concrete_table_queries(ctx, model) if is_update_sql(q["sql"]))


def count_select_for_update(ctx, model):
    return sum(1 for q in concrete_table_queries(ctx, model) if is_select_for_update_sql(q["sql"]))


def reversion_table_query_count(ctx):
    tables = (Revision._meta.db_table, Version._meta.db_table)
    return sum(1 for q in ctx.captured_queries if any(t in q["sql"] for t in tables))


class InvalidationTestCase(TestCase):
    """Shared snapshot payload for "content preserved" checks."""

    EXTRA_FIELDS = {
        Guide: {},
        Prompt: {},
        UseCase: {},
        Comparison: {},
    }
    SNAPSHOT_WITH_CONTENT = {"en": {"title": "Published title", "slug": "published-slug"}}


# ======================================================================
# Phase 3: result / error contract
# ======================================================================


class ResultAndErrorContractTests(TestCase):
    def test_result_is_frozen_and_slotted(self):
        result = ReviewInvalidationResult(
            changed=True,
            previous_status="review",
            current_status="draft",
            had_live_snapshot=False,
            no_op_reason=None,
        )
        with self.assertRaises(AttributeError):
            result.changed = False
        self.assertTrue(hasattr(type(result), "__slots__"))

    def test_error_codes_are_stable_distinct_strings(self):
        codes = {
            ReviewInvalidationErrorCode.UNSUPPORTED_OBJECT,
            ReviewInvalidationErrorCode.UNSAVED_OBJECT,
            ReviewInvalidationErrorCode.INVALID_DATABASE_ALIAS,
            ReviewInvalidationErrorCode.DATABASE_ALIAS_MISMATCH,
            ReviewInvalidationErrorCode.OBJECT_NOT_FOUND,
        }
        self.assertEqual(len(codes), 5)
        for code in codes:
            self.assertIsInstance(code, str)

    def test_no_op_reason_is_a_stable_string(self):
        self.assertEqual(
            ReviewInvalidationNoOpReason.STATUS_NOT_REVIEWABLE, "status_not_reviewable"
        )

    def test_error_carries_its_code(self):
        try:
            invalidate_editorial_review_state(None)
        except ReviewInvalidationError as exc:
            self.assertEqual(exc.code, ReviewInvalidationErrorCode.UNSUPPORTED_OBJECT)
            self.assertIsInstance(exc, ValueError)
        else:
            self.fail("expected ReviewInvalidationError")

    def test_changed_and_no_op_results_are_unambiguous(self):
        guide = Guide.objects.create(status=Workflow.STATUS_REVIEW, live_i18n={})
        changed = invalidate_editorial_review_state(guide)
        self.assertTrue(changed.changed)
        self.assertIsNone(changed.no_op_reason)
        self.assertNotEqual(changed.previous_status, changed.current_status)

        no_op = invalidate_editorial_review_state(refetch(guide))
        self.assertFalse(no_op.changed)
        self.assertEqual(no_op.no_op_reason, ReviewInvalidationNoOpReason.STATUS_NOT_REVIEWABLE)
        self.assertEqual(no_op.previous_status, no_op.current_status)


# ======================================================================
# Phase 4: internal FSM transitions
# ======================================================================


class InternalTransitionTests(TestCase):
    """Representative across all four concrete types, since the transitions
    live once on the shared abstract ``EditorialWorkflowMixin``."""

    def test_review_to_draft_and_review_to_rework_on_every_type(self):
        for model in EDITORIAL_TYPES:
            with self.subTest(model=model._meta.label, target="draft"):
                obj = model.objects.create(status=Workflow.STATUS_REVIEW)
                obj._invalidate_review_to_draft()
                self.assertEqual(obj.status, Workflow.STATUS_DRAFT)
            with self.subTest(model=model._meta.label, target="rework"):
                obj = model.objects.create(status=Workflow.STATUS_REVIEW)
                obj._invalidate_review_to_rework()
                self.assertEqual(obj.status, Workflow.STATUS_REWORK)

    def test_approved_to_draft_and_approved_to_rework_on_every_type(self):
        for model in EDITORIAL_TYPES:
            with self.subTest(model=model._meta.label, target="draft"):
                obj = model.objects.create(status=Workflow.STATUS_APPROVED)
                obj._invalidate_review_to_draft()
                self.assertEqual(obj.status, Workflow.STATUS_DRAFT)
            with self.subTest(model=model._meta.label, target="rework"):
                obj = model.objects.create(status=Workflow.STATUS_APPROVED)
                obj._invalidate_review_to_rework()
                self.assertEqual(obj.status, Workflow.STATUS_REWORK)

    def test_disallowed_sources_are_rejected_by_the_fsm_itself(self):
        from django_fsm import TransitionNotAllowed

        for status in UNTOUCHED_STATUSES:
            for method_name in ("_invalidate_review_to_draft", "_invalidate_review_to_rework"):
                with self.subTest(status=status, method=method_name):
                    guide = Guide.objects.create(status=status)
                    with self.assertRaises(TransitionNotAllowed):
                        getattr(guide, method_name)()

    def test_the_transition_methods_do_not_save(self):
        guide = Guide.objects.create(status=Workflow.STATUS_REVIEW)
        guide._invalidate_review_to_draft()
        # in-memory only - the DB row is untouched until something else saves
        self.assertEqual(refetch(guide).status, Workflow.STATUS_REVIEW)

    def test_the_transition_methods_do_not_touch_binding_or_reviewer_fields(self):
        revision = make_revision()
        guide = Guide.objects.create(
            status=Workflow.STATUS_REVIEW,
            review_revision=revision,
            approved_revision=revision,
            review_payload_fingerprint=fingerprint_review_payload({"a": 1}),
        )
        guide._invalidate_review_to_draft()
        self.assertEqual(guide.review_revision_id, revision.pk)
        self.assertEqual(guide.approved_revision_id, revision.pk)
        self.assertNotEqual(guide.review_payload_fingerprint, "")

    def test_no_new_revision_or_version_from_the_transition_alone(self):
        revisions_before = Revision.objects.count()
        versions_before = Version.objects.count()
        guide = Guide.objects.create(status=Workflow.STATUS_REVIEW)
        guide._invalidate_review_to_rework()
        self.assertEqual(Revision.objects.count(), revisions_before)
        self.assertEqual(Version.objects.count(), versions_before)


# ======================================================================
# Phase 6: per-type invalidation matrix
# ======================================================================


class PerTypeInvalidationMatrixTests(InvalidationTestCase):
    def _make_simple(self, model, *, status, has_snapshot):
        live_i18n = self.SNAPSHOT_WITH_CONTENT if has_snapshot else {}
        return model.objects.create(
            status=status,
            live_i18n=live_i18n,
            last_published_revision_id=555,
            review_note="feedback that must survive",
            submitted_for_review_at=timezone.now(),
            reviewed_at=timezone.now(),
        )

    def _make_comparison(self, *, status, has_snapshot):
        return Comparison.objects.create(
            status=status,
            live_i18n=self.SNAPSHOT_WITH_CONTENT if has_snapshot else {},
            live_entries=[] if has_snapshot else None,
            last_published_revision_id=555,
            review_note="feedback that must survive",
            submitted_for_review_at=timezone.now(),
            reviewed_at=timezone.now(),
        )

    def _make(self, model, *, status, has_snapshot):
        if model is Comparison:
            return self._make_comparison(status=status, has_snapshot=has_snapshot)
        return self._make_simple(model, status=status, has_snapshot=has_snapshot)

    def test_review_without_snapshot_becomes_draft(self):
        for model in EDITORIAL_TYPES:
            with self.subTest(model=model._meta.label):
                obj = self._make(model, status=Workflow.STATUS_REVIEW, has_snapshot=False)
                result = invalidate_editorial_review_state(obj)
                self.assertEqual(result.current_status, Workflow.STATUS_DRAFT)
                self.assertFalse(result.had_live_snapshot)
                self._assert_fields_cleared_and_content_preserved(
                    model, obj.pk, expected_live_entries=None
                )

    def test_approved_without_snapshot_becomes_draft(self):
        for model in EDITORIAL_TYPES:
            with self.subTest(model=model._meta.label):
                obj = self._make(model, status=Workflow.STATUS_APPROVED, has_snapshot=False)
                result = invalidate_editorial_review_state(obj)
                self.assertEqual(result.current_status, Workflow.STATUS_DRAFT)
                self._assert_fields_cleared_and_content_preserved(
                    model, obj.pk, expected_live_entries=None
                )

    def test_review_with_snapshot_becomes_rework(self):
        for model in EDITORIAL_TYPES:
            with self.subTest(model=model._meta.label):
                obj = self._make(model, status=Workflow.STATUS_REVIEW, has_snapshot=True)
                result = invalidate_editorial_review_state(obj)
                self.assertEqual(result.current_status, Workflow.STATUS_REWORK)
                self.assertTrue(result.had_live_snapshot)
                self._assert_fields_cleared_and_content_preserved(
                    model, obj.pk, expected_live_entries=[]
                )

    def test_approved_with_snapshot_becomes_rework(self):
        for model in EDITORIAL_TYPES:
            with self.subTest(model=model._meta.label):
                obj = self._make(model, status=Workflow.STATUS_APPROVED, has_snapshot=True)
                result = invalidate_editorial_review_state(obj)
                self.assertEqual(result.current_status, Workflow.STATUS_REWORK)
                self._assert_fields_cleared_and_content_preserved(
                    model, obj.pk, expected_live_entries=[]
                )

    def test_every_untouched_status_is_a_write_free_no_op(self):
        for model in EDITORIAL_TYPES:
            for status in UNTOUCHED_STATUSES:
                with self.subTest(model=model._meta.label, status=status):
                    obj = self._make(model, status=status, has_snapshot=True)
                    before = model.objects.get(pk=obj.pk)
                    before_snapshot = self._field_snapshot(before)

                    with CaptureQueriesContext(connection) as ctx:
                        result = invalidate_editorial_review_state(obj)

                    self.assertFalse(result.changed)
                    self.assertEqual(result.previous_status, status)
                    self.assertEqual(result.current_status, status)
                    self.assertEqual(
                        result.no_op_reason, ReviewInvalidationNoOpReason.STATUS_NOT_REVIEWABLE
                    )
                    self.assertEqual(count_updates(ctx, model), 0)

                    after = model.objects.get(pk=obj.pk)
                    self.assertEqual(self._field_snapshot(after), before_snapshot)

    # -- shared assertions --------------------------------------------

    def _field_snapshot(self, obj):
        data = {name: getattr(obj, name) for name in BINDING_FIELDS}
        data["status"] = obj.status
        data["review_note"] = obj.review_note
        data["updated_at"] = obj.updated_at
        data["live_i18n"] = obj.live_i18n
        data["last_published_revision_id"] = obj.last_published_revision_id
        if isinstance(obj, Comparison):
            data["live_entries"] = obj.live_entries
        return data

    def _assert_fields_cleared_and_content_preserved(self, model, pk, *, expected_live_entries=None):
        obj = model.objects.get(pk=pk)
        for name in BINDING_FIELDS:
            if name == "review_payload_fingerprint":
                continue
            self.assertIsNone(getattr(obj, name), name)
        self.assertEqual(obj.review_payload_fingerprint, "")
        self.assertEqual(obj.review_note, "feedback that must survive")
        self.assertEqual(obj.last_published_revision_id, 555)
        if isinstance(obj, Comparison):
            self.assertEqual(obj.live_entries, expected_live_entries)
        self.assertIsNotNone(obj.updated_at)


# ======================================================================
# Phase 7: Comparison special matrix
# ======================================================================


class ComparisonSpecialMatrixTests(TestCase):
    CASES = (
        ({}, None, Workflow.STATUS_DRAFT),
        ({}, [], Workflow.STATUS_DRAFT),
        ({"en": {"title": "T"}}, None, Workflow.STATUS_DRAFT),
        ({"en": {"title": "T"}}, [], Workflow.STATUS_REWORK),
        ({"en": {"title": "T"}}, [{"tool_id": 1, "position": 0, "translations": {}}], Workflow.STATUS_REWORK),
    )

    def test_the_full_matrix_from_review(self):
        for live_i18n, live_entries, expected in self.CASES:
            with self.subTest(live_i18n=live_i18n, live_entries=live_entries):
                obj = Comparison.objects.create(
                    status=Workflow.STATUS_REVIEW, live_i18n=live_i18n, live_entries=live_entries
                )
                result = invalidate_editorial_review_state(obj)
                self.assertEqual(result.current_status, expected)

    def test_the_boundary_cases_from_approved_too(self):
        boundary_cases = (
            ({"en": {"title": "T"}}, None, Workflow.STATUS_DRAFT),
            ({"en": {"title": "T"}}, [], Workflow.STATUS_REWORK),
        )
        for live_i18n, live_entries, expected in boundary_cases:
            with self.subTest(live_i18n=live_i18n, live_entries=live_entries):
                obj = Comparison.objects.create(
                    status=Workflow.STATUS_APPROVED, live_i18n=live_i18n, live_entries=live_entries
                )
                result = invalidate_editorial_review_state(obj)
                self.assertEqual(result.current_status, expected)

    def test_empty_entries_list_is_a_real_snapshot_not_a_missing_one(self):
        obj = Comparison.objects.create(
            status=Workflow.STATUS_REVIEW, live_i18n={"en": {"title": "T"}}, live_entries=[]
        )
        result = invalidate_editorial_review_state(obj)
        self.assertTrue(result.had_live_snapshot)
        self.assertEqual(result.current_status, Workflow.STATUS_REWORK)


# ======================================================================
# Phase 8: damaged bindings
# ======================================================================


class DamagedBindingTests(TestCase):
    def test_all_binding_fields_already_empty_still_invalidates(self):
        obj = Guide.objects.create(status=Workflow.STATUS_REVIEW)
        result = invalidate_editorial_review_state(obj)
        self.assertTrue(result.changed)
        reloaded = refetch(obj)
        self.assertIsNone(reloaded.review_revision_id)

    def test_empty_fingerprint_still_invalidates(self):
        revision = make_revision()
        obj = bound_object(
            Guide, status=Workflow.STATUS_REVIEW, revision=revision, fingerprint=""
        )
        result = invalidate_editorial_review_state(obj)
        self.assertTrue(result.changed)
        reloaded = refetch(obj)
        self.assertEqual(reloaded.review_payload_fingerprint, "")

    def test_syntactically_invalid_fingerprint_still_invalidates(self):
        revision = make_revision()
        obj = bound_object(
            Guide, status=Workflow.STATUS_REVIEW, revision=revision, fingerprint="not-a-real-digest"
        )
        result = invalidate_editorial_review_state(obj)
        self.assertTrue(result.changed)

    def test_foreign_review_revision_still_invalidates(self):
        foreign_prompt = create_with_revision(Prompt)
        foreign_revision = Revision.objects.get(
            version__object_id=str(foreign_prompt.pk), version__content_type__model="prompt"
        )
        obj = bound_object(Guide, status=Workflow.STATUS_REVIEW, revision=foreign_revision)
        result = invalidate_editorial_review_state(obj)
        self.assertTrue(result.changed)

    def test_foreign_approved_revision_still_invalidates(self):
        good_revision = make_revision()
        foreign_revision = make_revision("foreign")
        obj = Guide.objects.create(
            status=Workflow.STATUS_REVIEW,
            review_revision=good_revision,
            approved_revision=foreign_revision,
            review_payload_fingerprint=fingerprint_review_payload({"a": 1}),
        )
        result = invalidate_editorial_review_state(obj)
        self.assertTrue(result.changed)
        reloaded = refetch(obj)
        self.assertIsNone(reloaded.approved_revision_id)

    def test_mismatched_review_and_approved_revisions_still_invalidate(self):
        rev_a = make_revision("a")
        rev_b = make_revision("b")
        obj = Guide.objects.create(
            status=Workflow.STATUS_APPROVED,
            review_revision=rev_a,
            approved_revision=rev_b,
            review_payload_fingerprint=fingerprint_review_payload({"a": 1}),
        )
        result = invalidate_editorial_review_state(obj)
        self.assertTrue(result.changed)

    def test_only_review_revision_set_still_invalidates(self):
        revision = make_revision()
        obj = Guide.objects.create(
            status=Workflow.STATUS_REVIEW,
            review_revision=revision,
            review_payload_fingerprint=fingerprint_review_payload({"a": 1}),
        )
        result = invalidate_editorial_review_state(obj)
        self.assertTrue(result.changed)

    def test_only_approved_revision_set_still_invalidates(self):
        revision = make_revision()
        obj = Guide.objects.create(
            status=Workflow.STATUS_REVIEW,
            approved_revision=revision,
        )
        result = invalidate_editorial_review_state(obj)
        self.assertTrue(result.changed)

    def test_set_null_after_revision_deletion_still_invalidates(self):
        revision = make_revision()
        obj = bound_object(Guide, status=Workflow.STATUS_REVIEW, revision=revision)
        revision.delete()
        obj = refetch(obj)
        self.assertIsNone(obj.review_revision_id)
        self.assertIsNone(obj.approved_revision_id)
        result = invalidate_editorial_review_state(obj)
        self.assertTrue(result.changed)

    def test_all_damaged_states_end_with_every_binding_field_cleared(self):
        revision = make_revision()
        variants = [
            Guide.objects.create(status=Workflow.STATUS_REVIEW),
            bound_object(Guide, status=Workflow.STATUS_REVIEW, revision=revision, fingerprint=""),
            bound_object(Guide, status=Workflow.STATUS_APPROVED, revision=revision),
        ]
        for obj in variants:
            with self.subTest(pk=obj.pk):
                invalidate_editorial_review_state(obj)
                reloaded = refetch(obj)
                for name in BINDING_FIELDS:
                    if name == "review_payload_fingerprint":
                        continue
                    self.assertIsNone(getattr(reloaded, name), name)
                self.assertEqual(reloaded.review_payload_fingerprint, "")

    def test_no_binding_validation_function_is_ever_called(self):
        revision = make_revision()
        obj = bound_object(Guide, status=Workflow.STATUS_REVIEW, revision=revision)
        with mock.patch(
            "core.review_binding.validate_review_binding"
        ) as review_mock, mock.patch(
            "core.review_binding.validate_approved_binding"
        ) as approved_mock:
            invalidate_editorial_review_state(obj)
        review_mock.assert_not_called()
        approved_mock.assert_not_called()

    def test_binding_validation_would_have_rejected_these_but_invalidation_still_worked(self):
        """Cross-check against the real B2B1 validators, proving the damaged
        states above are genuinely damaged, not accidentally valid."""
        revision = make_revision()
        obj = bound_object(Guide, status=Workflow.STATUS_REVIEW, revision=revision, fingerprint="bad")
        self.assertFalse(validate_review_binding(obj).is_valid)
        self.assertFalse(validate_approved_binding(obj).is_valid)
        result = invalidate_editorial_review_state(obj)
        self.assertTrue(result.changed)


# ======================================================================
# Phase 9: stale caller state / DB source of truth
# ======================================================================


class StaleCallerStateTests(TestCase):
    def test_guide_db_gains_a_snapshot_after_the_caller_loaded_it(self):
        guide = Guide.objects.create(status=Workflow.STATUS_REVIEW, live_i18n={})
        stale = refetch(guide)  # local copy: no snapshot
        Guide.objects.filter(pk=guide.pk).update(
            live_i18n={"en": {"title": "T"}}, last_published_revision_id=1
        )
        # `stale` is not refreshed.
        self.assertEqual(stale.live_i18n, {})
        result = invalidate_editorial_review_state(stale)
        self.assertEqual(result.current_status, Workflow.STATUS_REWORK)
        self.assertTrue(result.had_live_snapshot)

    def test_guide_db_loses_its_snapshot_after_the_caller_loaded_it(self):
        guide = Guide.objects.create(
            status=Workflow.STATUS_REVIEW, live_i18n={"en": {"title": "T"}}
        )
        stale = refetch(guide)  # local copy: has a snapshot
        Guide.objects.filter(pk=guide.pk).update(live_i18n={})
        self.assertNotEqual(stale.live_i18n, {})
        result = invalidate_editorial_review_state(stale)
        self.assertEqual(result.current_status, Workflow.STATUS_DRAFT)
        self.assertFalse(result.had_live_snapshot)

    def test_db_status_change_to_published_wins_over_a_stale_local_review_status(self):
        guide = Guide.objects.create(status=Workflow.STATUS_REVIEW, live_i18n={})
        stale = refetch(guide)
        self.assertEqual(stale.status, Workflow.STATUS_REVIEW)
        Guide.objects.filter(pk=guide.pk).update(status=Workflow.STATUS_PUBLISHED)

        result = invalidate_editorial_review_state(stale)

        self.assertFalse(result.changed)
        self.assertEqual(result.previous_status, Workflow.STATUS_PUBLISHED)
        self.assertEqual(result.current_status, Workflow.STATUS_PUBLISHED)
        self.assertEqual(refetch(guide).status, Workflow.STATUS_PUBLISHED)

    def test_comparison_db_entries_null_to_empty_list_flips_the_target(self):
        comparison = Comparison.objects.create(
            status=Workflow.STATUS_REVIEW, live_i18n={"en": {"title": "T"}}, live_entries=None
        )
        stale = refetch(comparison)
        self.assertIsNone(stale.live_entries)
        Comparison.objects.filter(pk=comparison.pk).update(live_entries=[])

        result = invalidate_editorial_review_state(stale)
        self.assertEqual(result.current_status, Workflow.STATUS_REWORK)

    def test_comparison_db_entries_empty_list_to_null_flips_the_target(self):
        comparison = Comparison.objects.create(
            status=Workflow.STATUS_REVIEW, live_i18n={"en": {"title": "T"}}, live_entries=[]
        )
        stale = refetch(comparison)
        self.assertEqual(stale.live_entries, [])
        Comparison.objects.filter(pk=comparison.pk).update(live_entries=None)

        result = invalidate_editorial_review_state(stale)
        self.assertEqual(result.current_status, Workflow.STATUS_DRAFT)


# ======================================================================
# Phase 10: lock and query contract
# ======================================================================


class LockAndQueryContractTests(TestCase):
    def test_a_real_invalidation_locks_with_for_update_and_issues_one_update(self):
        guide = Guide.objects.create(status=Workflow.STATUS_REVIEW, live_i18n={})
        with CaptureQueriesContext(connection) as ctx:
            result = invalidate_editorial_review_state(guide)
        self.assertTrue(result.changed)
        self.assertEqual(count_select_for_update(ctx, Guide), 1)
        self.assertEqual(count_updates(ctx, Guide), 1)

    def test_a_no_op_locks_with_for_update_and_issues_no_update(self):
        guide = Guide.objects.create(status=Workflow.STATUS_DRAFT)
        with CaptureQueriesContext(connection) as ctx:
            result = invalidate_editorial_review_state(guide)
        self.assertFalse(result.changed)
        self.assertEqual(count_select_for_update(ctx, Guide), 1)
        self.assertEqual(count_updates(ctx, Guide), 0)

    def test_no_reversion_table_query_outside_an_active_revision_context(self):
        guide = Guide.objects.create(status=Workflow.STATUS_REVIEW, live_i18n={})
        with CaptureQueriesContext(connection) as ctx:
            invalidate_editorial_review_state(guide)
        self.assertEqual(reversion_table_query_count(ctx), 0)

    def test_no_child_or_translation_table_query_for_the_snapshot_decision(self):
        guide = Guide.objects.create(status=Workflow.STATUS_REVIEW, live_i18n={})
        guide.create_translation("en", title="T", intro="i", body="b", slug="b2b2-lockquery")
        section = GuideSection.objects.create(guide=guide, order=0)
        GuideItem.objects.create(section=section, kind="guide", order=0, url="https://example.com")

        with CaptureQueriesContext(connection) as ctx:
            invalidate_editorial_review_state(refetch(guide))

        for table in (
            guide.translations.model._meta.db_table,
            GuideSection._meta.db_table,
            GuideItem._meta.db_table,
        ):
            with self.subTest(table=table):
                self.assertFalse(any(table in q["sql"] for q in ctx.captured_queries))


# ======================================================================
# Phase 11: idempotence
# ======================================================================


class IdempotenceTests(TestCase):
    def _assert_idempotent(self, model, *, status):
        obj = model.objects.create(status=status, live_i18n={"en": {"title": "T"}})

        first = invalidate_editorial_review_state(obj)
        self.assertTrue(first.changed)

        after_first = model.objects.get(pk=obj.pk)
        revisions_before = Revision.objects.count()
        versions_before = Version.objects.count()
        updated_at_after_first = after_first.updated_at

        with CaptureQueriesContext(connection) as ctx:
            second = invalidate_editorial_review_state(after_first)

        self.assertFalse(second.changed)
        self.assertEqual(second.no_op_reason, ReviewInvalidationNoOpReason.STATUS_NOT_REVIEWABLE)
        self.assertEqual(second.previous_status, first.current_status)
        self.assertEqual(count_updates(ctx, model), 0)

        after_second = model.objects.get(pk=obj.pk)
        self.assertEqual(after_second.status, after_first.status)
        self.assertEqual(after_second.updated_at, updated_at_after_first)
        for name in BINDING_FIELDS:
            self.assertEqual(getattr(after_second, name), getattr(after_first, name))
        self.assertEqual(Revision.objects.count(), revisions_before)
        self.assertEqual(Version.objects.count(), versions_before)

    def test_idempotent_for_every_type_from_review(self):
        for model in EDITORIAL_TYPES:
            with self.subTest(model=model._meta.label):
                kwargs = {"status": Workflow.STATUS_REVIEW}
                self._assert_idempotent(model, **kwargs)

    def test_idempotent_from_approved(self):
        self._assert_idempotent(Guide, status=Workflow.STATUS_APPROVED)
        self._assert_idempotent(Comparison, status=Workflow.STATUS_APPROVED)


# ======================================================================
# Phase 12: no revision outside an active context
# ======================================================================


class NoRevisionOutsideContextTests(TestCase):
    def test_every_type_creates_no_revision_when_invalidated_standalone(self):
        for model in EDITORIAL_TYPES:
            with self.subTest(model=model._meta.label):
                obj = model.objects.create(status=Workflow.STATUS_REVIEW, live_i18n={})
                revisions_before = Revision.objects.count()
                versions_before = Version.objects.count()

                result = invalidate_editorial_review_state(obj)

                self.assertTrue(result.changed)
                self.assertEqual(Revision.objects.count(), revisions_before)
                self.assertEqual(Version.objects.count(), versions_before)
                self.assertEqual(refetch(obj).status, result.current_status)


# ======================================================================
# Phase 13: participates in an already-open revision context
# ======================================================================


class ExistingRevisionContextTests(TestCase):
    def test_prompt_invalidation_lands_in_the_callers_open_revision(self):
        revision = make_revision()
        prompt = bound_object(Prompt, status=Workflow.STATUS_REVIEW, revision=revision, live_i18n={})

        revisions_before = Revision.objects.count()

        with reversion.create_revision():
            result = invalidate_editorial_review_state(prompt)

        self.assertTrue(result.changed)
        self.assertEqual(Revision.objects.count(), revisions_before + 1)

        new_revision = Revision.objects.exclude(pk=revision.pk).latest("pk")
        prompt_ct_versions = new_revision.version_set.filter(content_type__model="prompt")
        self.assertEqual(prompt_ct_versions.count(), 1)
        version = prompt_ct_versions.get()
        self.assertEqual(version.object_id, str(prompt.pk))

        fields = json.loads(version.serialized_data)[0]["fields"]
        self.assertEqual(fields["status"], Workflow.STATUS_DRAFT)
        self.assertIsNone(fields["review_revision"])
        self.assertIsNone(fields["approved_revision"])
        self.assertEqual(fields["review_payload_fingerprint"], "")

    def test_comparison_invalidation_uses_the_b1_follow_graph_in_the_same_revision(self):
        tool = Tool.objects.create(slug="b2b2-tool")
        tool.create_translation("en", name="B2B2 Tool")

        revision = make_revision()
        comparison = bound_object(
            Comparison,
            status=Workflow.STATUS_APPROVED,
            revision=revision,
            live_i18n={"en": {"title": "T"}},
            live_entries=[],
        )
        entry = ComparisonToolEntry.objects.create(comparison=comparison, tool=tool, position=0)
        entry.create_translation("en", label="E", summary="s", pros="", cons="", special="")

        revisions_before = Revision.objects.count()

        with reversion.create_revision():
            result = invalidate_editorial_review_state(comparison)

        self.assertTrue(result.changed)
        self.assertEqual(Revision.objects.count(), revisions_before + 1)

        new_revision = Revision.objects.exclude(pk=revision.pk).latest("pk")
        labels = sorted(
            f"{v.content_type.app_label}.{v.content_type.model}"
            for v in new_revision.version_set.select_related("content_type")
        )
        # B1's follow graph: saving the parent inside an open revision block
        # pulls its already-existing tool_entries (and their translations)
        # into the same revision - this function never calls add_to_revision.
        self.assertIn("compare.comparison", labels)
        self.assertIn("compare.comparisontoolentry", labels)
        self.assertIn("compare.comparisontoolentrytranslation", labels)

        comparison_version = new_revision.version_set.get(
            content_type__model="comparison", object_id=str(comparison.pk)
        )
        fields = json.loads(comparison_version.serialized_data)[0]["fields"]
        self.assertEqual(fields["status"], Workflow.STATUS_REWORK)
        self.assertIsNone(fields["review_revision"])
        self.assertIsNone(fields["approved_revision"])


# ======================================================================
# Phase 14: rollback
# ======================================================================


class RollbackTests(TestCase):
    def test_save_failure_outside_a_revision_context_rolls_back_completely(self):
        guide = Guide.objects.create(status=Workflow.STATUS_REVIEW, live_i18n={})
        before = refetch(guide)
        revisions_before = Revision.objects.count()
        versions_before = Version.objects.count()

        with mock.patch.object(Guide, "save", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                invalidate_editorial_review_state(guide)

        after = refetch(guide)
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.status, Workflow.STATUS_REVIEW)
        self.assertEqual(after.updated_at, before.updated_at)
        self.assertIsNone(after.review_revision_id)
        self.assertEqual(Revision.objects.count(), revisions_before)
        self.assertEqual(Version.objects.count(), versions_before)

    def test_save_failure_inside_an_open_revision_context_persists_nothing(self):
        revision = make_revision()
        guide = bound_object(Guide, status=Workflow.STATUS_REVIEW, revision=revision, live_i18n={})
        before = refetch(guide)
        revisions_before = Revision.objects.count()
        versions_before = Version.objects.count()

        with self.assertRaises(RuntimeError):
            with reversion.create_revision():
                with mock.patch.object(Guide, "save", side_effect=RuntimeError("boom")):
                    invalidate_editorial_review_state(guide)

        after = refetch(guide)
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.review_revision_id, before.review_revision_id)
        self.assertEqual(after.review_payload_fingerprint, before.review_payload_fingerprint)
        self.assertEqual(after.review_note, before.review_note)
        self.assertEqual(after.updated_at, before.updated_at)
        self.assertEqual(Revision.objects.count(), revisions_before)
        self.assertEqual(Version.objects.count(), versions_before)

    def test_review_note_and_content_survive_a_rollback(self):
        guide = Guide.objects.create(
            status=Workflow.STATUS_REVIEW, live_i18n={}, review_note="do not lose me"
        )
        with mock.patch.object(Guide, "save", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                invalidate_editorial_review_state(guide)
        self.assertEqual(refetch(guide).review_note, "do not lose me")


# ======================================================================
# Phase 15: input and alias errors
# ======================================================================


class InputAndAliasErrorTests(TestCase):
    def test_none_is_rejected(self):
        with self.assertRaises(ReviewInvalidationError) as ctx:
            invalidate_editorial_review_state(None)
        self.assertEqual(ctx.exception.code, ReviewInvalidationErrorCode.UNSUPPORTED_OBJECT)

    def test_unrelated_model_is_rejected(self):
        tool = Tool.objects.create(slug="b2b2-unsupported")
        with self.assertRaises(ReviewInvalidationError) as ctx:
            invalidate_editorial_review_state(tool)
        self.assertEqual(ctx.exception.code, ReviewInvalidationErrorCode.UNSUPPORTED_OBJECT)

    def test_translation_model_is_rejected(self):
        guide = Guide.objects.create()
        guide.create_translation("en", title="T", intro="i", body="b", slug="b2b2-translation")
        translation = guide.translations.get(language_code="en")
        with self.assertRaises(ReviewInvalidationError) as ctx:
            invalidate_editorial_review_state(translation)
        self.assertEqual(ctx.exception.code, ReviewInvalidationErrorCode.UNSUPPORTED_OBJECT)

    def test_child_model_is_rejected(self):
        guide = Guide.objects.create()
        section = GuideSection.objects.create(guide=guide, order=0)
        with self.assertRaises(ReviewInvalidationError) as ctx:
            invalidate_editorial_review_state(section)
        self.assertEqual(ctx.exception.code, ReviewInvalidationErrorCode.UNSUPPORTED_OBJECT)

    def test_queryset_is_rejected(self):
        Guide.objects.create()
        with self.assertRaises(ReviewInvalidationError) as ctx:
            invalidate_editorial_review_state(Guide.objects.all())
        self.assertEqual(ctx.exception.code, ReviewInvalidationErrorCode.UNSUPPORTED_OBJECT)

    def test_list_is_rejected(self):
        guide = Guide.objects.create()
        with self.assertRaises(ReviewInvalidationError) as ctx:
            invalidate_editorial_review_state([guide])
        self.assertEqual(ctx.exception.code, ReviewInvalidationErrorCode.UNSUPPORTED_OBJECT)

    def test_unsaved_root_object_is_rejected(self):
        with self.assertRaises(ReviewInvalidationError) as ctx:
            invalidate_editorial_review_state(Guide())
        self.assertEqual(ctx.exception.code, ReviewInvalidationErrorCode.UNSAVED_OBJECT)

    def test_deleted_root_row_raises_object_not_found(self):
        guide = Guide.objects.create(status=Workflow.STATUS_REVIEW)
        pk = guide.pk
        Guide.objects.filter(pk=pk).delete()
        with self.assertRaises(ReviewInvalidationError) as ctx:
            invalidate_editorial_review_state(guide)
        self.assertEqual(ctx.exception.code, ReviewInvalidationErrorCode.OBJECT_NOT_FOUND)

    def test_invalid_using_type_raises_type_error(self):
        guide = Guide.objects.create()
        with self.assertRaises(TypeError):
            invalidate_editorial_review_state(guide, using=123)

    def test_unknown_alias_name_raises_invalid_database_alias(self):
        guide = Guide.objects.create()
        with self.assertRaises(ReviewInvalidationError) as ctx:
            invalidate_editorial_review_state(guide, using="not-a-real-alias")
        self.assertEqual(ctx.exception.code, ReviewInvalidationErrorCode.INVALID_DATABASE_ALIAS)

    def test_using_never_silently_falls_back_to_default_on_an_unknown_alias(self):
        guide = Guide.objects.create(status=Workflow.STATUS_REVIEW)
        with self.assertRaises(ReviewInvalidationError):
            invalidate_editorial_review_state(guide, using="bogus")
        self.assertEqual(refetch(guide).status, Workflow.STATUS_REVIEW)

    def test_explicit_using_contradicting_the_objects_own_alias_is_a_mismatch(self):
        guide = Guide.objects.create()
        guide._state.db = "not-default"
        with self.assertRaises(ReviewInvalidationError) as ctx:
            invalidate_editorial_review_state(guide, using=DEFAULT_DB_ALIAS)
        self.assertEqual(ctx.exception.code, ReviewInvalidationErrorCode.DATABASE_ALIAS_MISMATCH)

    def test_explicit_using_matching_the_objects_own_alias_is_accepted(self):
        guide = Guide.objects.create(status=Workflow.STATUS_REVIEW)
        self.assertEqual(guide._state.db, DEFAULT_DB_ALIAS)
        result = invalidate_editorial_review_state(guide, using=DEFAULT_DB_ALIAS)
        self.assertTrue(result.changed)

    def test_early_errors_perform_zero_queries(self):
        guide = Guide.objects.create()
        with self.assertNumQueries(0):
            with self.assertRaises(ReviewInvalidationError):
                invalidate_editorial_review_state(None)
        with self.assertNumQueries(0):
            with self.assertRaises(ReviewInvalidationError):
                invalidate_editorial_review_state(Guide())
        with self.assertNumQueries(0):
            with self.assertRaises(ReviewInvalidationError):
                invalidate_editorial_review_state(guide, using="bogus-alias")
        with self.assertNumQueries(0):
            with self.assertRaises(ReviewInvalidationError):
                guide._state.db = "not-default"
                invalidate_editorial_review_state(guide, using=DEFAULT_DB_ALIAS)


# ======================================================================
# Phase 16: no runtime activation
# ======================================================================


class NoRuntimeActivationTests(TestCase):
    def test_production_definitions_exist_only_in_the_two_expected_modules(self):
        import ast
        import pathlib

        import core.models.editorial as editorial_module
        import core.review_binding as review_binding_module

        symbols = (
            "invalidate_editorial_review_state",
            "_invalidate_review_to_draft",
            "_invalidate_review_to_rework",
        )

        allowed_definition_files = {
            pathlib.Path(editorial_module.__file__),
            pathlib.Path(review_binding_module.__file__),
        }

        project_root = pathlib.Path(review_binding_module.__file__).resolve().parents[1]
        offenders = []
        for py_file in project_root.rglob("*.py"):
            if "venv" in py_file.parts or "migrations" in py_file.parts:
                continue
            if "/tests/" in str(py_file) or py_file.name.startswith("test_"):
                continue
            text = py_file.read_text(encoding="utf-8", errors="ignore")
            if not any(symbol in text for symbol in symbols):
                continue
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in symbols:
                    if py_file.resolve() not in allowed_definition_files:
                        offenders.append(f"definition of {node.name} in {py_file}")

        self.assertEqual(offenders, [])

    def test_admin_modules_never_call_the_new_primitive_or_transitions(self):
        import compare.admin
        import guides.admin
        import prompts.admin
        import usecases.admin

        for module in (guides.admin, prompts.admin, usecases.admin, compare.admin):
            source = open(module.__file__, encoding="utf-8").read()
            for symbol in (
                "invalidate_editorial_review_state",
                "_invalidate_review_to_draft",
                "_invalidate_review_to_rework",
            ):
                with self.subTest(module=module.__name__, symbol=symbol):
                    self.assertNotIn(symbol, source)


class B2ARuntimeNonActivationStillHoldsTests(TestCase):
    """
    A cheap, local re-assertion of the existing B2A runtime contract, so this
    module can be run standalone and still catch an accidental wiring
    regression without depending on test-module load order.
    """

    def test_submit_still_leaves_binding_fields_untouched(self):
        guide = Guide.objects.create(status=Workflow.STATUS_DRAFT)
        guide.move_to_review(by=None)
        guide.save(update_fields=["status", "submitted_for_review_at", "updated_at"])
        reloaded = refetch(guide)
        self.assertEqual(reloaded.status, Workflow.STATUS_REVIEW)
        self.assertIsNone(reloaded.review_revision_id)
        self.assertEqual(reloaded.review_payload_fingerprint, "")
