"""
Beta 11.11C4G: a reusable, two-phase primitive for detecting whether a
Prompt's canonical review payload actually changed across an edit, and
automatically invalidating a bound review/approval when it did.

Not activated by this slice
-----------------------------
Nothing in production calls either function yet - not ``prompts/admin.py``,
not ``content/views/editorial.py``, not translation-delete, not taggit, not
the tool m2m, not any signal. This module exists purely as a tested,
importable primitive for a later edit-guard integration slice. See
``prompts/tests/test_review_edit_guard.py``'s static check that proves it.

The two phases
---------------
:func:`capture_prompt_review_edit_baseline` reads the *current* canonical
Beta 11.11C1/C4D v2 payload fingerprint of a Prompt, before a caller performs
some content mutation (edit a translation, add/remove a tag, add/remove a
tool, reassign the author, ...). :func:`invalidate_prompt_review_if_payload_changed`
is called afterwards, in the same transaction, rebuilds the payload fresh and
compares. Only a genuine fingerprint difference triggers exactly one call to
the existing Beta 11.11B2B2 primitive,
``core.review_binding.invalidate_editorial_review_state()`` - never a second,
parallel implementation of what "invalidatable status", "target status", or
"which fields get cleared" means. An unchanged fingerprint is a true no-op:
no B2B2 call, no save, no revision.

    with transaction.atomic(using=alias):
        baseline = capture_prompt_review_edit_baseline(prompt, using=alias)
        ... the caller's own content mutation happens here ...
        result = invalidate_prompt_review_if_payload_changed(
            prompt, baseline=baseline, using=alias,
        )

Why this needs no transaction of its own
-------------------------------------------
Both phases must run inside a transaction the *caller* already opened -
enforced by checking ``connections[alias].in_atomic_block`` before any query,
never by opening ``transaction.atomic()`` here. That is what lets the root
lock :func:`capture_prompt_review_edit_baseline` takes stay held for the
caller's own mutation and for the compare phase's re-check, and what lets
C2A/C3A and future edit paths serialize against each other through the same
row. Neither function opens a savepoint, a batch wrapper, or a
``transaction.on_commit`` hook, and neither opens a ``reversion.create_revision()``
context of its own - an already-active caller-owned reversion context is
explicitly allowed (unlike C2A/C3A's submit/approve primitives, which reject
one, because those own their entire operation end-to-end; this guard is
meant to be called *from inside* a caller's own revision block).

The transaction-identity boundary this API cannot close
-----------------------------------------------------------
Public Django has no supported way to prove two calls ran inside the exact
same outermost atomic block - only that each one currently is inside *some*
atomic block on the expected alias, for the expected prompt. This module
therefore checks what it can (alias, concrete prompt id, an active atomic
block, and the baseline's own structural validity) and no more; it does not
reach into ``connection.atomic_blocks``, savepoint ids, or ``Atomic`` object
identity to manufacture a stronger guarantee. A baseline captured in one
transaction and compared in a later, separate one can, at worst, cause a
conservative invalidation (a false "changed") - the compare phase always
rebuilds the payload fresh from the database and can never legitimize an
actually-different payload as unchanged just because a stale baseline says
so. Misusing the two phases across unrelated transactions is a documented API
boundary, not a defect this slice can close.

What this module reuses, and refuses to duplicate
-----------------------------------------------------
* Payload construction: ``prompts.review_payload.build_prompt_review_payload()``
  (Beta 11.11C1/C4D) - the only place that knows the v2 payload shape.
* Fingerprinting: ``core.review_binding.fingerprint_review_payload()`` - the
  only place that knows the canonical JSON/SHA-256 contract.
* Invalidation: ``core.review_binding.invalidate_editorial_review_state()``
  (Beta 11.11B2B2) - the only place that decides which statuses are
  invalidatable, the review/rework/draft target-status rule, which fields get
  cleared, and its own save/no-op contract. This module never reimplements
  any of that, and never touches ``prompt.save()``, a bulk
  ``QuerySet.update()``, taggit, the tool m2m, or a reversion context itself
  - see ``prompts/tests/test_review_edit_guard.py``'s static safety checks.
"""
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from django.db import DEFAULT_DB_ALIAS, connections

from core.review_binding import (
    ReviewInvalidationNoOpReason,
    ReviewInvalidationResult,
    fingerprint_review_payload,
    has_provable_live_snapshot,
    invalidate_editorial_review_state,
)
from prompts.models import Prompt
from prompts.review_payload import build_prompt_review_payload

#: Frozen, local reproduction of ``core.review_binding._is_valid_sha256_hexdigest`` -
#: that helper is module-private by convention, and this format check is a
#: trivial, self-contained syntactic property (not the hashing/canonicalization
#: logic itself, which is never duplicated here - see the module docstring).
_LOWERCASE_HEX_DIGITS = frozenset("0123456789abcdef")


def _is_valid_sha256_hexdigest(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    if len(value) != 64:
        return False
    return all(char in _LOWERCASE_HEX_DIGITS for char in value)


class PromptReviewEditGuardErrorCode(StrEnum):
    """Stable reason the guard refused to run, independent of message wording."""

    UNSUPPORTED_OBJECT = "unsupported_object"
    UNSAVED_OBJECT = "unsaved_object"
    INVALID_DATABASE_ALIAS = "invalid_database_alias"
    DATABASE_ALIAS_MISMATCH = "database_alias_mismatch"
    ATOMIC_CONTEXT_REQUIRED = "atomic_context_required"
    OBJECT_NOT_FOUND = "object_not_found"
    INVALID_BASELINE = "invalid_baseline"
    BASELINE_PROMPT_MISMATCH = "baseline_prompt_mismatch"
    BASELINE_DATABASE_ALIAS_MISMATCH = "baseline_database_alias_mismatch"
    BASELINE_SCHEMA_MISMATCH = "baseline_schema_mismatch"
    BASELINE_FINGERPRINT_INVALID = "baseline_fingerprint_invalid"


class PromptReviewEditGuardError(ValueError):
    """Raised by either guard phase for every input, alias, atomic-context or
    baseline problem it refuses to proceed past. Carries a stable
    :attr:`code`, mirroring the other ``review_*`` modules. Errors raised by
    the reused C1 payload builder or the reused B2B2 invalidation primitive
    are never caught or reinterpreted here - they propagate unchanged."""

    def __init__(self, code: PromptReviewEditGuardErrorCode, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class PromptReviewEditBaseline:
    """
    Immutable, minimal record of a Prompt's canonical review payload state at
    one moment - never the payload itself, never a model instance.

    ``payload_schema`` and ``payload_fingerprint`` are read verbatim off
    whatever :func:`prompts.review_payload.build_prompt_review_payload`
    actually produced; this dataclass never hardcodes or re-derives the
    schema string itself. Carries no user, translation, or revision
    reference, and no workflow status - the baseline represents the payload,
    not the row's broader state.
    """

    prompt_id: int
    database_alias: str
    payload_schema: str
    payload_fingerprint: str


@dataclass(frozen=True, slots=True)
class PromptReviewEditGuardResult:
    """
    Immutable outcome of :func:`invalidate_prompt_review_if_payload_changed`.

    ``payload_changed`` is the guard's own fingerprint comparison;
    ``invalidated`` is whether the reused B2B2 primitive actually changed the
    row (``False`` both when the payload never changed at all, and when it
    changed but the row's status was not invalidatable - distinguish the two
    via ``payload_changed``). ``previous_status``/``current_status``/
    ``had_live_snapshot``/``no_op_reason`` mirror B2B2's own
    :class:`~core.review_binding.ReviewInvalidationResult` when B2B2 ran, or
    the freshly-read row's own (unchanged) state when it did not - never an
    independently re-derived decision. ``invalidation`` carries that full
    B2B2 result verbatim (``None`` when the payload did not change, since
    B2B2 was never called).
    """

    prompt_id: int
    database_alias: str
    baseline_fingerprint: str
    current_fingerprint: str
    payload_changed: bool
    invalidated: bool
    previous_status: str
    current_status: str
    had_live_snapshot: bool
    no_op_reason: ReviewInvalidationNoOpReason | None
    invalidation: ReviewInvalidationResult | None


def _is_concrete_prompt(prompt: Any) -> bool:
    """
    Mirrors ``content/views/editorial.py``'s own ``_is_concrete_prompt()``
    idiom (Beta 11.11C4B): a proxy of Prompt counts, a same-named unrelated
    object or a genuine (non-proxy) Prompt subclass does not. Never a class
    name string, a URL slug, duck typing, or a check for individual methods.
    Safe for any input, including ``None`` or a non-model object.
    """
    meta = getattr(prompt, "_meta", None)
    if meta is None:
        return False
    return meta.concrete_model is Prompt


def _validate_prompt_type_and_pk(prompt: Any) -> None:
    if not _is_concrete_prompt(prompt):
        raise PromptReviewEditGuardError(
            PromptReviewEditGuardErrorCode.UNSUPPORTED_OBJECT,
            "prompts.review_edit_guard only accepts a prompts.Prompt instance, got "
            f"{'None' if prompt is None else type(prompt).__name__}",
        )
    if prompt.pk is None:
        raise PromptReviewEditGuardError(
            PromptReviewEditGuardErrorCode.UNSAVED_OBJECT,
            "prompts.review_edit_guard requires a saved object with a primary key",
        )


def _resolve_database_alias(prompt: Prompt, using: str | None) -> str:
    """
    Alias resolution + validation, in the fixed order C2A/C3A/B2B2 all use:
    explicit ``using``, then ``prompt._state.db``, then ``DEFAULT_DB_ALIAS``.
    An unknown alias name is always ``INVALID_DATABASE_ALIAS`` (checked
    before the mismatch check), so it is never misreported as a mere
    conflict with the object's own alias. No cross-database fallback, no
    alias sourced from request data, no hardcoded ``"default"``.
    """
    obj_db_alias = getattr(prompt._state, "db", None)

    if using is not None:
        if using not in connections:
            raise PromptReviewEditGuardError(
                PromptReviewEditGuardErrorCode.INVALID_DATABASE_ALIAS,
                f"{using!r} is not a configured database alias",
            )
        if obj_db_alias and obj_db_alias != using:
            raise PromptReviewEditGuardError(
                PromptReviewEditGuardErrorCode.DATABASE_ALIAS_MISMATCH,
                f"explicit using={using!r} does not match the object's own database "
                f"alias {obj_db_alias!r}",
            )
        return using

    db_alias = obj_db_alias or DEFAULT_DB_ALIAS
    if db_alias not in connections:
        raise PromptReviewEditGuardError(
            PromptReviewEditGuardErrorCode.INVALID_DATABASE_ALIAS,
            f"{db_alias!r} is not a configured database alias",
        )
    return db_alias


def _require_active_atomic_block(db_alias: str) -> None:
    """
    Fail-closed, no automatic ``transaction.atomic()`` opened as a
    convenience. Checked before any query, lock, or payload build in either
    phase - see the module docstring for why this API owns no transaction of
    its own.
    """
    if not connections[db_alias].in_atomic_block:
        raise PromptReviewEditGuardError(
            PromptReviewEditGuardErrorCode.ATOMIC_CONTEXT_REQUIRED,
            "prompts.review_edit_guard must be called inside an already-active "
            f"transaction.atomic(using={db_alias!r}) block",
        )


def _lock_prompt_root(prompt_id: int, db_alias: str) -> Prompt:
    """Fresh, unfiltered, locked root - the same concrete manager C2A/C3A/B2B2
    all use, never the public visibility-filtered manager, never a
    language-scoped query."""
    try:
        return (
            Prompt._default_manager.using(db_alias)
            .select_for_update()
            .get(pk=prompt_id)
        )
    except Prompt.DoesNotExist:
        raise PromptReviewEditGuardError(
            PromptReviewEditGuardErrorCode.OBJECT_NOT_FOUND,
            f"prompts.Prompt #{prompt_id} no longer exists",
        ) from None


def capture_prompt_review_edit_baseline(
    prompt: Any, *, using: str | None = None
) -> PromptReviewEditBaseline:
    """
    Phase 1: record the canonical v2 payload fingerprint of ``prompt`` as it
    stands *right now*, before the caller's own content mutation.

    Must be called inside an already-active ``transaction.atomic(using=...)``
    block - see the module docstring. Locks the concrete Prompt root fresh
    via ``SELECT ... FOR UPDATE`` (through the unfiltered manager, no public
    visibility filter, no language scoping), builds the payload purely
    through :func:`prompts.review_payload.build_prompt_review_payload`, and
    fingerprints it purely through
    :func:`core.review_binding.fingerprint_review_payload`.

    Never changes status, never touches a binding field, never bumps
    ``updated_at``, never creates a revision or version, never reads any
    account attribute the C1 payload does not itself need, and never calls
    the B2B2 invalidation primitive. Raises
    :class:`PromptReviewEditGuardError` (stable :class:`PromptReviewEditGuardErrorCode`)
    for every input, alias, atomic-context or lookup problem; propagates any
    error the reused payload builder itself raises, unchanged.
    """
    _validate_prompt_type_and_pk(prompt)
    if using is not None and not isinstance(using, str):
        raise TypeError(f"using must be a database alias string, got {type(using).__name__}")
    db_alias = _resolve_database_alias(prompt, using)
    _require_active_atomic_block(db_alias)

    locked = _lock_prompt_root(prompt.pk, db_alias)

    payload = build_prompt_review_payload(locked, using=db_alias)
    fingerprint = fingerprint_review_payload(payload)

    return PromptReviewEditBaseline(
        prompt_id=locked.pk,
        database_alias=db_alias,
        payload_schema=payload["schema"],
        payload_fingerprint=fingerprint,
    )


def _validate_baseline(
    baseline: Any, *, prompt_id: int, database_alias: str
) -> None:
    """Purely structural, no query - checked before the compare phase ever
    touches the database, so a garbage baseline fails closed without taking
    a lock or building a payload."""
    if not isinstance(baseline, PromptReviewEditBaseline):
        raise PromptReviewEditGuardError(
            PromptReviewEditGuardErrorCode.INVALID_BASELINE,
            "baseline must be a PromptReviewEditBaseline instance, got "
            f"{type(baseline).__name__}",
        )
    if baseline.prompt_id != prompt_id:
        raise PromptReviewEditGuardError(
            PromptReviewEditGuardErrorCode.BASELINE_PROMPT_MISMATCH,
            f"baseline belongs to prompt #{baseline.prompt_id}, not #{prompt_id}",
        )
    if baseline.database_alias != database_alias:
        raise PromptReviewEditGuardError(
            PromptReviewEditGuardErrorCode.BASELINE_DATABASE_ALIAS_MISMATCH,
            f"baseline belongs to database alias {baseline.database_alias!r}, not "
            f"{database_alias!r}",
        )
    if not _is_valid_sha256_hexdigest(baseline.payload_fingerprint):
        raise PromptReviewEditGuardError(
            PromptReviewEditGuardErrorCode.BASELINE_FINGERPRINT_INVALID,
            "baseline.payload_fingerprint is not a valid lowercase SHA-256 hex digest",
        )


def invalidate_prompt_review_if_payload_changed(
    prompt: Any, *, baseline: PromptReviewEditBaseline, using: str | None = None
) -> PromptReviewEditGuardResult:
    """
    Phase 2: rebuild ``prompt``'s canonical v2 payload fresh and compare it
    against ``baseline``. Calls the existing Beta 11.11B2B2 primitive,
    :func:`core.review_binding.invalidate_editorial_review_state`, exactly
    once if - and only if - the fingerprints differ; a true no-op otherwise.

    Must be called inside an already-active ``transaction.atomic(using=...)``
    block, on the same concrete prompt and database alias the baseline was
    captured against. Locks the root fresh via ``SELECT ... FOR UPDATE`` -
    independently of whatever lock :func:`capture_prompt_review_edit_baseline`
    took, so the compare phase stays fail-closed even if it somehow runs
    against a different underlying transaction than capture did (see the
    module docstring's transaction-identity boundary). A second lock of the
    same row within the same PostgreSQL transaction is a normal, cheap no-op;
    B2B2's own lock afterwards is a third, likewise harmless, re-lock.

    Never performs its own content, status, or binding mutation - the only
    write this function can ever cause is B2B2's own single ``save()``, and
    only after a real fingerprint difference. Never opens a transaction,
    savepoint, ``on_commit`` hook, or reversion context of its own; an
    already-active caller-owned reversion context is allowed and is where
    B2B2's own save, if any, is recorded.

    Raises :class:`PromptReviewEditGuardError` for every input, alias,
    atomic-context, baseline, or lookup problem; propagates any error the
    reused payload builder or the reused B2B2 primitive itself raises,
    unchanged - never reinterpreted as a harmless no-op.
    """
    _validate_prompt_type_and_pk(prompt)
    if using is not None and not isinstance(using, str):
        raise TypeError(f"using must be a database alias string, got {type(using).__name__}")
    db_alias = _resolve_database_alias(prompt, using)
    _require_active_atomic_block(db_alias)

    _validate_baseline(baseline, prompt_id=prompt.pk, database_alias=db_alias)

    locked = _lock_prompt_root(prompt.pk, db_alias)

    payload = build_prompt_review_payload(locked, using=db_alias)
    current_schema = payload["schema"]
    if current_schema != baseline.payload_schema:
        raise PromptReviewEditGuardError(
            PromptReviewEditGuardErrorCode.BASELINE_SCHEMA_MISMATCH,
            f"current payload schema {current_schema!r} does not match the baseline's "
            f"{baseline.payload_schema!r}",
        )

    current_fingerprint = fingerprint_review_payload(payload)
    payload_changed = current_fingerprint != baseline.payload_fingerprint

    if not payload_changed:
        return PromptReviewEditGuardResult(
            prompt_id=locked.pk,
            database_alias=db_alias,
            baseline_fingerprint=baseline.payload_fingerprint,
            current_fingerprint=current_fingerprint,
            payload_changed=False,
            invalidated=False,
            previous_status=locked.status,
            current_status=locked.status,
            had_live_snapshot=has_provable_live_snapshot(locked),
            no_op_reason=None,
            invalidation=None,
        )

    invalidation = invalidate_editorial_review_state(locked, using=db_alias)

    return PromptReviewEditGuardResult(
        prompt_id=locked.pk,
        database_alias=db_alias,
        baseline_fingerprint=baseline.payload_fingerprint,
        current_fingerprint=current_fingerprint,
        payload_changed=True,
        invalidated=invalidation.changed,
        previous_status=invalidation.previous_status,
        current_status=invalidation.current_status,
        had_live_snapshot=invalidation.had_live_snapshot,
        no_op_reason=invalidation.no_op_reason,
        invalidation=invalidation,
    )
