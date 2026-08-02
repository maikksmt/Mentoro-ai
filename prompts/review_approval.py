"""
Beta 11.11C3A: atomic, per-root prompt review approval.

``approve_prompt_review`` takes exactly one saved Prompt from ``review`` to
``approved`` and binds ``approved_revision`` to *the exact same*
django-reversion revision ``review_revision`` already points at - the one
Beta 11.11C2A captured at submit time. Everything happens inside one outer
``transaction.atomic(using=…)`` around a ``SELECT ... FOR UPDATE``-locked
root; any failure rolls the whole thing back, leaving the row exactly as it
was and leaving no orphan revision or version behind.

Approval validates and confirms, it never restores
--------------------------------------------------
Unlike a revert, this primitive never applies a stored version, reconstructs
a translation, or rebuilds tag/tool membership from history. It only checks
that the *current*, already-stored content still fingerprints identically to
what was submitted (via ``prompts.review_payload.build_prompt_review_payload``
and ``core.review_binding.fingerprint_review_payload`` - the same Beta
11.11C1 payload C2A used) and, if so, confirms that content as approved. The
database must already equal the reviewed state; this function does not make
it so.

Why ``approved_revision`` equals ``review_revision``, never a new revision
----------------------------------------------------------------------------
``review_revision`` is the content that was actually reviewed. Approval
confirms *that* content, not a new snapshot of whatever the row happens to
serialize to at approval time (which is not meant to differ anyway - that is
exactly what the fingerprint re-checks below rule out). The ``save()`` this
function performs only changes workflow columns (status, approval binding,
reviewer, timestamp), so the revision django-reversion captures for it is a
pure workflow-audit graph, never a source of truth for what was approved and
never consulted to determine ``approved_revision`` - see
``prompts/review_submission.py``'s module docstring for why a
"newest revision" heuristic (``Revision.objects.last()``, ``order_by("-id")``,
a max-id or timestamp query) is forbidden in this codebase; here it is not
just forbidden but unnecessary, because unlike a submit - where the revision
does not exist until the save happens - the revision being bound
(``review_revision``) is already known before this function's own save runs.
No signal receiver, no ``ContextVar`` token, no capture step of any kind.

Central binding validation, not a parallel implementation
-----------------------------------------------------------
The structural review-binding check (revision present, fingerprint present
and syntactically valid, revision actually belongs to this root) is not
reimplemented here: it is delegated verbatim to
``core.review_binding.validate_review_binding()`` (Beta 11.11B2B1), and its
:class:`~core.review_binding.BindingFailureReason` is carried through on
:attr:`PromptReviewApprovalError.binding_reason`. The one pre-state check that
validator does not cover - ``approved_revision`` already set while the row is
still ``review`` - is Beta 11.11C3A's own approval-specific addition (see
:func:`approve_prompt_review`), also reported as
:data:`PromptReviewApprovalErrorCode.REVIEW_BINDING_INVALID`, but with
``binding_reason`` left ``None`` since no central reason code describes it.

Consistency boundary (deliberately not closed here, same as C2A)
--------------------------------------------------------------------
Only the prompt *root* is locked with ``SELECT ... FOR UPDATE``. The C1
payload additionally reads translations, tag membership, tool membership and
the author's public attributes, which are not locked. The root lock is the
intended synchronization point, and the before/after fingerprint re-check
(against the stored ``review_payload_fingerprint``, then again against the
row after this function's own save) catches any change that becomes visible
during approval. Full serialization with the C4 edit paths, which will take
the same root lock, remains future work. This slice introduces no global
advisory lock, no serializable isolation, no repeatable-read switch, and no
generic locking engine.

Known graph gap carried over unchanged (Beta 11.11B1 / C1 / C2A)
--------------------------------------------------------------------
The approval-audit revision this function's save produces contains the
prompt root and its Parler translations (the same
``core.reversion_registration`` follow-graph every editorial save already
uses) - it does *not* contain taggit membership or prompt-tool membership.
Both are part of the C1 fingerprint this function checks, so a stale tag or
tool change still blocks approval even though it never appears in any
revision's version set.

Not activated by this slice
-----------------------------
Nothing calls this function yet: not ``prompts/admin.py``'s
``action_approve`` (still the shared, un-bound
``core.admin.EditorialWorkflowAdminMixin`` path), not bulk approval, not
publish, not any other editorial type's admin. See
``prompts/tests/test_review_approval.py`` for the static check that proves
it.
"""
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import reversion
from django.contrib.auth import get_user_model
from django.db import DEFAULT_DB_ALIAS, connections, transaction

from core.review_binding import (
    BindingFailureReason,
    fingerprint_review_payload,
    validate_review_binding,
)
from prompts.models import Prompt
from prompts.review_payload import build_prompt_review_payload

User = get_user_model()

#: The stable, English revision comment the existing shared admin approve
#: action already uses (``core.admin.EditorialWorkflowAdminMixin.action_approve``,
#: via ``reversion.set_comment("approve")``). Reused verbatim so this
#: primitive and the admin write the same comment; no request data, language
#: or random marker ever enters the persisted comment.
APPROVE_REVISION_COMMENT = "approve"

#: The only status this primitive will approve from. Deliberately the single
#: real source of the FSM ``approve`` transition itself
#: (``EditorialWorkflowMixin.approve``, ``source=STATUS_REVIEW``) - not a
#: wider gate that happens to also be technically reachable through the FSM.
_APPROVABLE_STATUS = Prompt.STATUS_REVIEW


class PromptReviewApprovalErrorCode(StrEnum):
    """Stable reason :func:`approve_prompt_review` refused to run or aborted,
    independent of message wording."""

    UNSUPPORTED_OBJECT = "unsupported_object"
    UNSAVED_OBJECT = "unsaved_object"
    INVALID_DATABASE_ALIAS = "invalid_database_alias"
    DATABASE_ALIAS_MISMATCH = "database_alias_mismatch"
    OBJECT_NOT_FOUND = "object_not_found"
    INVALID_ACTOR = "invalid_actor"
    ACTOR_DATABASE_ALIAS_MISMATCH = "actor_database_alias_mismatch"
    ACTIVE_REVERSION_CONTEXT = "active_reversion_context"
    STATUS_NOT_APPROVABLE = "status_not_approvable"
    REVIEW_BINDING_INVALID = "review_binding_invalid"
    REVIEW_PAYLOAD_CHANGED = "review_payload_changed"
    PAYLOAD_CHANGED_DURING_APPROVAL = "payload_changed_during_approval"


class PromptReviewApprovalError(ValueError):
    """
    Raised by :func:`approve_prompt_review` for every input, alias, actor,
    status, binding or consistency problem it refuses to proceed past.

    Carries a stable :attr:`code`, mirroring the other ``review_*`` modules.
    For :data:`PromptReviewApprovalErrorCode.REVIEW_BINDING_INVALID` raised
    from the central :func:`core.review_binding.validate_review_binding`
    check, :attr:`binding_reason` carries that validator's own
    :class:`~core.review_binding.BindingFailureReason`; for the one
    approval-specific pre-state check it does not cover (an ``approved_revision``
    already set while the row is still ``review``), and for every other error
    code, :attr:`binding_reason` is ``None``.
    """

    def __init__(
        self,
        code: PromptReviewApprovalErrorCode,
        message: str,
        *,
        binding_reason: BindingFailureReason | None = None,
    ):
        self.code = code
        self.binding_reason = binding_reason
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class PromptReviewApprovalResult:
    """
    Immutable outcome of a successful :func:`approve_prompt_review`.

    Carries only plain scalars - never a ``Prompt`` or ``Revision`` instance,
    so nothing here can trigger a lazy query after the call returns.

    ``previous_status`` is always ``"review"``; ``current_status`` is always
    ``"approved"``; ``review_revision_id`` and ``approved_revision_id`` are
    always equal - the same revision C2A bound at submit time;
    ``fingerprint`` equals the (unchanged) ``review_payload_fingerprint``;
    ``database_alias`` is the alias every query ran against.
    """

    prompt_id: int
    previous_status: str
    current_status: str
    review_revision_id: int
    approved_revision_id: int
    fingerprint: str
    database_alias: str


def _resolve_database_alias(prompt: Prompt, using: str | None) -> str:
    """
    Alias resolution + validation, in the fixed order the C1 builder and C2A
    both use: an unknown alias name is always ``INVALID_DATABASE_ALIAS``
    (checked before the mismatch check), so it is never misreported as a mere
    conflict with the object's own alias. No cross-database fallback.
    """
    obj_db_alias = getattr(prompt._state, "db", None)

    if using is not None:
        if using not in connections:
            raise PromptReviewApprovalError(
                PromptReviewApprovalErrorCode.INVALID_DATABASE_ALIAS,
                f"{using!r} is not a configured database alias",
            )
        if obj_db_alias and obj_db_alias != using:
            raise PromptReviewApprovalError(
                PromptReviewApprovalErrorCode.DATABASE_ALIAS_MISMATCH,
                f"explicit using={using!r} does not match the object's own database "
                f"alias {obj_db_alias!r}",
            )
        return using

    db_alias = obj_db_alias or DEFAULT_DB_ALIAS
    if db_alias not in connections:
        raise PromptReviewApprovalError(
            PromptReviewApprovalErrorCode.INVALID_DATABASE_ALIAS,
            f"{db_alias!r} is not a configured database alias",
        )
    return db_alias


def _validate_actor(actor: Any, db_alias: str) -> None:
    """
    Unlike C2A's optional ``actor``, approval requires one: only a *saved*
    instance of the real ``AUTH_USER_MODEL``, on a database alias that does
    not contradict the resolved approval alias. Staff/superuser/permission
    checks are deliberately out of scope - those arrive with the admin wiring
    in a later slice. This never reads ``request.user``; the caller passes
    the actor explicitly.
    """
    if actor is None or not isinstance(actor, User) or actor.pk is None:
        raise PromptReviewApprovalError(
            PromptReviewApprovalErrorCode.INVALID_ACTOR,
            "actor is required and must be a saved AUTH_USER_MODEL instance, got "
            f"{'None' if actor is None else type(actor).__name__}",
        )
    actor_db_alias = getattr(actor._state, "db", None)
    if actor_db_alias and actor_db_alias != db_alias:
        raise PromptReviewApprovalError(
            PromptReviewApprovalErrorCode.ACTOR_DATABASE_ALIAS_MISMATCH,
            f"actor is bound to database alias {actor_db_alias!r}, not the "
            f"approval alias {db_alias!r}",
        )


def approve_prompt_review(
    prompt: Any, *, actor: Any, using: str | None = None
) -> PromptReviewApprovalResult:
    """
    Atomically approve exactly one saved Prompt that is currently ``review``
    with a valid, unchanged review binding; see the module docstring for the
    full contract. Accepts only a saved ``prompts.Prompt`` (or a proxy
    resolving to it); ``actor`` is a required saved user recorded as both the
    FSM's reviewer and the approval revision's author. Returns a
    :class:`PromptReviewApprovalResult` on success and raises
    :class:`PromptReviewApprovalError` (with a stable
    :class:`PromptReviewApprovalErrorCode`) on every refusal or abort - never
    a bare no-op, and never a swallowed exception.
    """
    # 1-2. Object type and primary key.
    if prompt is None or not isinstance(prompt, Prompt):
        raise PromptReviewApprovalError(
            PromptReviewApprovalErrorCode.UNSUPPORTED_OBJECT,
            "approve_prompt_review() only accepts a prompts.Prompt instance, got "
            f"{'None' if prompt is None else type(prompt).__name__}",
        )
    if prompt.pk is None:
        raise PromptReviewApprovalError(
            PromptReviewApprovalErrorCode.UNSAVED_OBJECT,
            "approve_prompt_review() requires a saved object with a primary key",
        )

    # 3-5. Database alias (type check, then existence, then mismatch).
    if using is not None and not isinstance(using, str):
        raise TypeError(f"using must be a database alias string, got {type(using).__name__}")
    db_alias = _resolve_database_alias(prompt, using)

    # 6. Actor - required, unlike C2A.
    _validate_actor(actor, db_alias)

    # 7. Reject an already-active reversion context: this approval must get
    # its own revision graph, never a nested or shared one. Before any query
    # or mutation.
    if reversion.is_active():
        raise PromptReviewApprovalError(
            PromptReviewApprovalErrorCode.ACTIVE_REVERSION_CONTEXT,
            "approve_prompt_review() must not run inside an active "
            "reversion.create_revision() block",
        )

    # 8. Outer transaction - the sole rollback boundary.
    with transaction.atomic(using=db_alias):
        # 9. Fresh, locked root through the unfiltered manager.
        try:
            locked = (
                Prompt._default_manager.using(db_alias)
                .select_for_update()
                .get(pk=prompt.pk)
            )
        except Prompt.DoesNotExist:
            raise PromptReviewApprovalError(
                PromptReviewApprovalErrorCode.OBJECT_NOT_FOUND,
                f"prompts.Prompt #{prompt.pk} no longer exists",
            ) from None

        # 10. Approvability decided on the locked database row, never the
        # caller - and deliberately narrower than the FSM's own source list
        # would technically allow.
        previous_status = locked.status
        if previous_status != _APPROVABLE_STATUS:
            raise PromptReviewApprovalError(
                PromptReviewApprovalErrorCode.STATUS_NOT_APPROVABLE,
                f"prompt #{locked.pk} is in status {previous_status!r}, which cannot "
                "be approved",
            )

        # 11. Central B2B1 structural binding check - never reimplemented.
        binding_result = validate_review_binding(locked, using=db_alias)
        if not binding_result.is_valid:
            raise PromptReviewApprovalError(
                PromptReviewApprovalErrorCode.REVIEW_BINDING_INVALID,
                f"prompt #{locked.pk} has no valid review binding: "
                f"{binding_result.reason}",
                binding_reason=binding_result.reason,
            )

        # 12. Approval-specific pre-state check the central validator does
        # not cover: a review-status row must not already carry an approval
        # binding. Fail-closed rather than silently overwriting it.
        if locked.approved_revision_id is not None:
            raise PromptReviewApprovalError(
                PromptReviewApprovalErrorCode.REVIEW_BINDING_INVALID,
                f"prompt #{locked.pk} already has approved_revision "
                f"#{locked.approved_revision_id} set while still in "
                f"{previous_status!r}",
            )

        # 13-15. Current C1 payload + fingerprint, checked against the
        # binding stored at submit time. review_revision is already known
        # good from step 11 - no second lookup, no heuristic.
        stored_fingerprint = locked.review_payload_fingerprint
        payload_before = build_prompt_review_payload(locked, using=db_alias)
        current_fingerprint = fingerprint_review_payload(payload_before)
        if current_fingerprint != stored_fingerprint:
            raise PromptReviewApprovalError(
                PromptReviewApprovalErrorCode.REVIEW_PAYLOAD_CHANGED,
                f"prompt #{locked.pk}'s content changed since it was submitted "
                "for review",
            )

        # 16-21. Exactly one own reversion context. atomic=False: the outer
        # transaction above is the rollback boundary; a nested atomic would
        # only add a savepoint. This is a pure workflow-audit revision - it is
        # never consulted to determine approved_revision (see module
        # docstring).
        with reversion.create_revision(atomic=False, using=db_alias):
            reversion.set_user(actor)
            reversion.set_comment(APPROVE_REVISION_COMMENT)

            # 18. Real FSM transition - never a bare status assignment
            # (status is a protected FSMField). No note, so review_note is
            # left exactly as stored. The transition itself sets reviewed_at
            # and reviewed_by; not duplicated here.
            locked.approve(by=actor)

            # 19. The exact revision already bound at submit time - never a
            # fresh lookup, never this save's own (about-to-be-created)
            # revision.
            locked.approved_revision = locked.review_revision

            # 21. Exactly one approval save.
            locked.save(
                update_fields=[
                    "status",
                    "approved_revision",
                    "reviewed_by",
                    "reviewed_at",
                    "updated_at",
                ]
            )

        # 23-25. Re-verify the payload fingerprint against the state now on
        # record. Excluded workflow fields (status, fingerprint, bindings,
        # reviewer metadata) do not enter the payload, so unchanged content
        # fingerprints identically before and after the approval save; a real
        # content change racing in during approval does not.
        payload_after = build_prompt_review_payload(locked, using=db_alias)
        fingerprint_after = fingerprint_review_payload(payload_after)
        if fingerprint_after != stored_fingerprint:
            raise PromptReviewApprovalError(
                PromptReviewApprovalErrorCode.PAYLOAD_CHANGED_DURING_APPROVAL,
                f"prompt #{locked.pk}'s content changed while the approval was "
                "in progress",
            )

        # 26-27. Structured result; the outer transaction commits on exit.
        return PromptReviewApprovalResult(
            prompt_id=locked.pk,
            previous_status=previous_status,
            current_status=locked.status,
            review_revision_id=locked.review_revision_id,
            approved_revision_id=locked.approved_revision_id,
            fingerprint=stored_fingerprint,
            database_alias=db_alias,
        )
