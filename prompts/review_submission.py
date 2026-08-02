"""
Beta 11.11C2A: atomic, per-root prompt review submission.

``submit_prompt_for_review`` takes exactly one saved Prompt from ``draft`` or
``rework`` into ``review`` and binds it to the *exact* django-reversion
revision it produced, plus the canonical Beta 11.11C1 payload fingerprint of
its stored draft. Everything happens inside one outer
``transaction.atomic(using=…)`` around a ``SELECT ... FOR UPDATE``-locked root;
any failure rolls the whole thing back, leaving the row - status, bindings,
reviewer metadata, review note, timestamps - exactly as it was, and leaving no
orphan revision or version behind.

Why this is a separate primitive from the admin action
------------------------------------------------------
``core.admin.EditorialWorkflowAdminMixin.action_submit_for_review`` wraps a
*whole changelist selection* in a single shared ``reversion.create_revision()``
- one revision for the batch. That is exactly the "which revision was this
review bound to?" ambiguity Beta 11.11A flagged. C2A gives one prompt its own
isolated revision graph and records that graph unambiguously, without touching
the admin action yet (that wiring is C2B). Until then nothing in production
calls this function - see ``prompts/tests/test_review_submission.py`` for the
static check that proves it.

How the exact revision is captured (concurrency-safe)
-----------------------------------------------------
django-reversion sends ``reversion.signals.post_revision_commit`` synchronously
when a ``create_revision()`` context closes, carrying the just-saved
``revision`` (already has a pk) and its ``versions`` (each carrying ``.db``),
while still inside the surrounding transaction (verified empirically before
this slice was written). Rather than any "newest revision" heuristic
(``Revision.objects.last()``, ``order_by("-id")``, max-id, timestamp - all
forbidden here because a concurrent commit could win the race), this connects a
*per-call* receiver, keyed by a unique ``dispatch_uid`` and gated by a
per-call token published in a ``ContextVar``:

* Django signals are process-global, so during two concurrent submissions both
  receivers are connected at once and *both* fire for either commit. The
  ``ContextVar`` is what disambiguates: ``post_revision_commit`` fires in the
  same execution context that opened the ``create_revision`` block, so
  ``_active_submission_token.get()`` inside the receiver returns *that call's*
  token - the other call's receiver sees a different token and ignores the
  signal. The expected database alias is checked as a second gate.
* The receiver and the ContextVar token are both torn down in ``finally`` -
  after success, after any exception, and whether the signal fired once, not at
  all, or (defensively) more than once - so nothing leaks between calls.

After the context closes exactly one revision must have been captured
(``REVISION_NOT_CAPTURED`` / ``MULTIPLE_REVISIONS_CAPTURED`` otherwise), it must
contain a root version of this prompt (``revision_contains_object`` →
``ROOT_VERSION_MISSING`` otherwise), and the C1 payload must fingerprint
identically before and after the revision save
(``PAYLOAD_CHANGED_DURING_SUBMISSION`` otherwise). Only then is
``review_revision`` bound, in a second targeted save *outside* the reversion
context (so it produces no second version) but *inside* the same outer
transaction.

Consistency boundary (deliberately not closed here)
---------------------------------------------------
Only the prompt *root* is locked with ``SELECT ... FOR UPDATE``. The C1 payload
additionally reads translations, tag membership, tool membership and the
author's public attributes, which are not locked. The root lock is the intended
synchronization point, and the before/after fingerprint re-check catches any
change that becomes visible during this submission. A change committed after
the second verification cannot be fully serialized until the C4 edit paths take
the same root lock. This slice introduces no global advisory lock, no
serializable isolation, no repeatable-read switch, and no generic locking
engine.

Known graph gaps carried over unchanged (Beta 11.11B1 / C1)
-----------------------------------------------------------
The captured revision contains the prompt root and its Parler translations. It
does *not* contain taggit membership or prompt-tool membership - those are part
of the C1 fingerprint but not of the B1 reversion follow graph, and C2A neither
rebuilds the manifest nor publishes via revert. A later approval/publish guard
compares the freshly rebuilt current payload, which is why a tag or tool change
still changes the fingerprint even though it is absent from the revision.
"""
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import reversion
from django.contrib.auth import get_user_model
from django.db import DEFAULT_DB_ALIAS, connections, transaction
from reversion.signals import post_revision_commit

from core.review_binding import fingerprint_review_payload, revision_contains_object
from prompts.models import Prompt
from prompts.review_payload import build_prompt_review_payload

User = get_user_model()

#: The stable, English revision comment the existing admin submit action
#: already uses (``core.admin.action_submit_for_review``). Reused verbatim so
#: this primitive and the admin write the same comment; no request data,
#: language or random marker ever enters the persisted comment.
SUBMIT_REVISION_COMMENT = "submit_for_review"

#: The only statuses this primitive will submit. Deliberately stricter than the
#: FSM ``move_to_review`` transition, whose source list also includes
#: ``published`` and ``approved`` (left untouched): a *submission* is only
#: meaningful from a draft-side state. Every other status is
#: :data:`PromptReviewSubmissionErrorCode.STATUS_NOT_SUBMITTABLE`.
_SUBMITTABLE_STATUSES = (Prompt.STATUS_DRAFT, Prompt.STATUS_REWORK)

#: Identifies the currently-running submission call within its execution
#: context, so the per-call ``post_revision_commit`` receiver captures only the
#: revision this call produced. See the module docstring.
_active_submission_token: ContextVar[str | None] = ContextVar(
    "prompt_submission_token", default=None
)


class PromptReviewSubmissionErrorCode(StrEnum):
    """Stable reason :func:`submit_prompt_for_review` refused to run or aborted,
    independent of message wording."""

    UNSUPPORTED_OBJECT = "unsupported_object"
    UNSAVED_OBJECT = "unsaved_object"
    INVALID_DATABASE_ALIAS = "invalid_database_alias"
    DATABASE_ALIAS_MISMATCH = "database_alias_mismatch"
    OBJECT_NOT_FOUND = "object_not_found"
    INVALID_ACTOR = "invalid_actor"
    ACTOR_DATABASE_ALIAS_MISMATCH = "actor_database_alias_mismatch"
    ACTIVE_REVERSION_CONTEXT = "active_reversion_context"
    STATUS_NOT_SUBMITTABLE = "status_not_submittable"
    REVISION_NOT_CAPTURED = "revision_not_captured"
    MULTIPLE_REVISIONS_CAPTURED = "multiple_revisions_captured"
    ROOT_VERSION_MISSING = "root_version_missing"
    PAYLOAD_CHANGED_DURING_SUBMISSION = "payload_changed_during_submission"


class PromptReviewSubmissionError(ValueError):
    """Raised by :func:`submit_prompt_for_review` for every input, alias, actor,
    status, revision-capture or consistency problem it refuses to proceed past.
    Carries a stable :attr:`code`, mirroring the other ``review_*`` modules."""

    def __init__(self, code: PromptReviewSubmissionErrorCode, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class PromptReviewSubmissionResult:
    """
    Immutable outcome of a successful :func:`submit_prompt_for_review`.

    Carries only plain scalars - never a ``Prompt`` or ``Revision`` instance,
    so nothing here can trigger a lazy query after the call returns.

    ``previous_status`` is ``"draft"`` or ``"rework"``; ``current_status`` is
    always ``"review"``; ``revision_id`` equals the row's new
    ``review_revision_id``; ``fingerprint`` equals its new
    ``review_payload_fingerprint``; ``database_alias`` is the alias every query
    ran against.
    """

    prompt_id: int
    previous_status: str
    current_status: str
    revision_id: int
    fingerprint: str
    database_alias: str


def _resolve_database_alias(prompt: Prompt, using: str | None) -> str:
    """
    Alias resolution + validation, in the fixed order the C1 builder uses: an
    unknown alias name is always ``INVALID_DATABASE_ALIAS`` (checked before the
    mismatch check), so it is never misreported as a mere conflict with the
    object's own alias. No cross-database fallback.
    """
    obj_db_alias = getattr(prompt._state, "db", None)

    if using is not None:
        if using not in connections:
            raise PromptReviewSubmissionError(
                PromptReviewSubmissionErrorCode.INVALID_DATABASE_ALIAS,
                f"{using!r} is not a configured database alias",
            )
        if obj_db_alias and obj_db_alias != using:
            raise PromptReviewSubmissionError(
                PromptReviewSubmissionErrorCode.DATABASE_ALIAS_MISMATCH,
                f"explicit using={using!r} does not match the object's own database "
                f"alias {obj_db_alias!r}",
            )
        return using

    db_alias = obj_db_alias or DEFAULT_DB_ALIAS
    if db_alias not in connections:
        raise PromptReviewSubmissionError(
            PromptReviewSubmissionErrorCode.INVALID_DATABASE_ALIAS,
            f"{db_alias!r} is not a configured database alias",
        )
    return db_alias


def _validate_actor(actor: Any, db_alias: str) -> None:
    """
    ``actor`` is optional. When given it must be a *saved* instance of the real
    ``AUTH_USER_MODEL`` on a database alias that does not contradict the
    resolved submission alias. Staff/superuser/permission checks are
    deliberately out of scope - those arrive with the admin wiring in C2B. This
    never reads ``request.user``; the caller passes the actor explicitly.
    """
    if actor is None:
        return
    if not isinstance(actor, User) or actor.pk is None:
        raise PromptReviewSubmissionError(
            PromptReviewSubmissionErrorCode.INVALID_ACTOR,
            "actor must be None or a saved AUTH_USER_MODEL instance, got "
            f"{type(actor).__name__}",
        )
    actor_db_alias = getattr(actor._state, "db", None)
    if actor_db_alias and actor_db_alias != db_alias:
        raise PromptReviewSubmissionError(
            PromptReviewSubmissionErrorCode.ACTOR_DATABASE_ALIAS_MISMATCH,
            f"actor is bound to database alias {actor_db_alias!r}, not the "
            f"submission alias {db_alias!r}",
        )


def submit_prompt_for_review(
    prompt: Any, *, actor: Any = None, using: str | None = None
) -> PromptReviewSubmissionResult:
    """
    Atomically submit exactly one saved Prompt for review; see the module
    docstring for the full contract. Accepts only a saved ``prompts.Prompt``
    (or a proxy resolving to it); ``actor`` is an optional saved user recorded
    as the revision's author. Returns a :class:`PromptReviewSubmissionResult`
    on success and raises :class:`PromptReviewSubmissionError` (with a stable
    :class:`PromptReviewSubmissionErrorCode`) on every refusal or abort - never
    a bare no-op, and never a swallowed exception.
    """
    # 1-2. Object type and primary key.
    if prompt is None or not isinstance(prompt, Prompt):
        raise PromptReviewSubmissionError(
            PromptReviewSubmissionErrorCode.UNSUPPORTED_OBJECT,
            "submit_prompt_for_review() only accepts a prompts.Prompt instance, got "
            f"{'None' if prompt is None else type(prompt).__name__}",
        )
    if prompt.pk is None:
        raise PromptReviewSubmissionError(
            PromptReviewSubmissionErrorCode.UNSAVED_OBJECT,
            "submit_prompt_for_review() requires a saved object with a primary key",
        )

    # 3. Database alias (type check, then existence, then mismatch).
    if using is not None and not isinstance(using, str):
        raise TypeError(f"using must be a database alias string, got {type(using).__name__}")
    db_alias = _resolve_database_alias(prompt, using)

    # 4. Actor.
    _validate_actor(actor, db_alias)

    # 5. Reject an already-active reversion context: every prompt must get its
    # own revision graph, never a nested or shared one. Before any query or
    # mutation.
    if reversion.is_active():
        raise PromptReviewSubmissionError(
            PromptReviewSubmissionErrorCode.ACTIVE_REVERSION_CONTEXT,
            "submit_prompt_for_review() must not run inside an active "
            "reversion.create_revision() block",
        )

    call_token = uuid.uuid4().hex
    dispatch_uid = f"prompts.review_submission:{call_token}"
    captured_revisions: list = []

    def _capture_receiver(sender, revision, versions, **kwargs):
        # Only this call's own revision, in this execution context, on the
        # expected alias. A concurrent submission's commit fires in a different
        # execution context, so its token differs and this returns early.
        if _active_submission_token.get() != call_token:
            return
        if not any(getattr(version, "db", None) == db_alias for version in versions):
            return
        captured_revisions.append(revision)

    # 6. Outer transaction - the sole rollback boundary.
    with transaction.atomic(using=db_alias):
        # 7. Fresh, locked root through the unfiltered manager.
        try:
            locked = (
                Prompt._default_manager.using(db_alias)
                .select_for_update()
                .get(pk=prompt.pk)
            )
        except Prompt.DoesNotExist:
            raise PromptReviewSubmissionError(
                PromptReviewSubmissionErrorCode.OBJECT_NOT_FOUND,
                f"prompts.Prompt #{prompt.pk} no longer exists",
            ) from None

        # 8. Submittability decided on the locked database row, never the caller.
        previous_status = locked.status
        if previous_status not in _SUBMITTABLE_STATUSES:
            raise PromptReviewSubmissionError(
                PromptReviewSubmissionErrorCode.STATUS_NOT_SUBMITTABLE,
                f"prompt #{locked.pk} is in status {previous_status!r}, which cannot "
                "be submitted for review",
            )

        # 10-11. Canonical C1 payload + fingerprint of the stored draft.
        payload_before = build_prompt_review_payload(locked, using=db_alias)
        fingerprint_before = fingerprint_review_payload(payload_before)

        # 12. Exactly one own reversion context. atomic=False: the outer
        # transaction above is the rollback boundary; a nested atomic would
        # only add a savepoint.
        variable_token = _active_submission_token.set(call_token)
        post_revision_commit.connect(_capture_receiver, dispatch_uid=dispatch_uid, weak=False)
        try:
            with reversion.create_revision(atomic=False, using=db_alias):
                if actor is not None:
                    reversion.set_user(actor)
                reversion.set_comment(SUBMIT_REVISION_COMMENT)

                # 13. Real FSM transition - never a bare status assignment
                # (status is a protected FSMField). No note, so review_note is
                # left exactly as stored.
                locked.move_to_review(by=actor)

                # 9. Fail-closed cleanup of any old/broken binding, plus the new
                # fingerprint. review_revision stays None in this save - it is
                # bound only after the revision is captured, so the root version
                # in this revision is deliberately not self-referential.
                locked.review_revision = None
                locked.approved_revision = None
                locked.review_payload_fingerprint = fingerprint_before
                locked.reviewed_by = None
                locked.reviewed_at = None

                # 14. Exactly one submit save. review_note is absent from
                # update_fields, matching the existing admin contract.
                locked.save(
                    update_fields=[
                        "status",
                        "submitted_for_review_at",
                        "review_revision",
                        "approved_revision",
                        "review_payload_fingerprint",
                        "reviewed_by",
                        "reviewed_at",
                        "updated_at",
                    ]
                )
        finally:
            post_revision_commit.disconnect(_capture_receiver, dispatch_uid=dispatch_uid)
            _active_submission_token.reset(variable_token)

        # 17. Exactly one revision.
        if not captured_revisions:
            raise PromptReviewSubmissionError(
                PromptReviewSubmissionErrorCode.REVISION_NOT_CAPTURED,
                "no revision was captured for this submission",
            )
        if len(captured_revisions) > 1:
            raise PromptReviewSubmissionError(
                PromptReviewSubmissionErrorCode.MULTIPLE_REVISIONS_CAPTURED,
                f"{len(captured_revisions)} revisions were captured for this submission",
            )
        captured_revision = captured_revisions[0]

        # 18. The captured revision must contain a root version of this prompt,
        # on this alias (revision_contains_object filters by db=alias, so a
        # foreign-alias revision fails this too).
        if not revision_contains_object(captured_revision, locked, using=db_alias):
            raise PromptReviewSubmissionError(
                PromptReviewSubmissionErrorCode.ROOT_VERSION_MISSING,
                f"captured revision #{captured_revision.pk} does not contain a root "
                f"version of prompt #{locked.pk}",
            )

        # 19-21. Re-verify the payload fingerprint against the state now on
        # record. Excluded workflow fields (status, fingerprint, bindings) do
        # not enter the payload, so an unchanged draft fingerprints identically
        # before and after the submit save; a real content change does not.
        payload_after = build_prompt_review_payload(locked, using=db_alias)
        fingerprint_after = fingerprint_review_payload(payload_after)
        if fingerprint_after != fingerprint_before:
            raise PromptReviewSubmissionError(
                PromptReviewSubmissionErrorCode.PAYLOAD_CHANGED_DURING_SUBMISSION,
                "the review payload changed while the submission was in progress",
            )

        # 22-23. Bind review_revision in a second targeted save - outside the
        # reversion context (no second version) but inside the outer
        # transaction. updated_at is deliberately not re-touched here.
        locked.review_revision = captured_revision
        locked.save(update_fields=["review_revision"])

        # 24-25. Structured result; the outer transaction commits on exit.
        return PromptReviewSubmissionResult(
            prompt_id=locked.pk,
            previous_status=previous_status,
            current_status=locked.status,
            revision_id=captured_revision.pk,
            fingerprint=fingerprint_before,
            database_alias=db_alias,
        )
