"""
Beta 11.11B2B1: pure, read-only review-binding primitives.

Beta 11.11B2A added three columns to every editorial model -
``review_revision``, ``approved_revision``, ``review_payload_fingerprint`` -
and deliberately left them unwritten: nothing in the runtime sets them yet.
Before any slice starts writing to them (a submit-time revision, an approval
binding, a publish guard, automatic invalidation), the project needs a small,
independently testable vocabulary for what a *valid* binding even means. This
module is that vocabulary and nothing else:

* :func:`fingerprint_review_payload` - canonical SHA-256 of an already-built
  JSON payload.
* :func:`revision_contains_object` - does a ``reversion.Revision`` actually
  contain a root version of this object?
* :func:`validate_review_binding` / :func:`validate_approved_binding` - is the
  binding on this object syntactically and relationally sound?
* :func:`has_provable_live_snapshot` / :func:`target_status_after_review_invalidation`
  - the B2A live-snapshot and target-status rules, as pure functions instead of
  migration-only code.

Every function here is read-only: no ``save()``, no ``update()``, no status
mutation, no revision creation, no signals. Nothing in this module is called
from an admin action, a workflow method, or a service yet - see the module
docstring of ``core/reversion_registration.py`` and the Beta 11.11B2A report
for what is intentionally still missing. Wiring these primitives into submit,
approve, publish and invalidation is later slices' job.
"""
import hashlib
import json
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from django.db import DEFAULT_DB_ALIAS
from reversion.models import Revision, Version

from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin
from guides.models import Guide
from prompts.models import Prompt
from usecases.models import UseCase

#: The four concrete editorial types this module knows the live-snapshot and
#: root-object contract for. Deliberately a closed, explicit set - see
#: :func:`has_provable_live_snapshot` - rather than a generic "any model with
#: a live_i18n field" heuristic, which could quietly accept an unrelated model
#: that happens to share a field name.
_SIMPLE_LIVE_SNAPSHOT_TYPES = (Guide, Prompt, UseCase)


# ======================================================================
# 1. Canonical review-payload fingerprint
# ======================================================================


class ReviewFingerprintErrorCode(StrEnum):
    """Stable reason a payload was refused, independent of message wording."""

    UNSUPPORTED_TYPE = "unsupported_type"
    NON_STRING_DICT_KEY = "non_string_dict_key"
    NON_FINITE_FLOAT = "non_finite_float"


class ReviewPayloadFingerprintError(ValueError):
    """
    Raised by :func:`fingerprint_review_payload` when the payload contains a
    value that cannot be canonically and losslessly represented as JSON.

    Carries a stable :attr:`code` so callers can branch on the failure kind
    without parsing the message text.
    """

    def __init__(self, code: ReviewFingerprintErrorCode, message: str):
        self.code = code
        super().__init__(message)


def _reject_json_unsafe_values(value: Any, *, path: str) -> None:
    """
    Recursively rejects anything :func:`fingerprint_review_payload` must not
    silently accept - it never converts or coerces, only validates.

    Order matters: ``bool`` is a subclass of ``int`` in Python, so it is
    checked first, and ``json.dumps`` already serializes ``True``/``1`` to
    distinct tokens (``true``/``1``), so no special handling of that pair is
    needed beyond letting normal JSON semantics apply.

    Dict keys are the one place ``json.dumps`` would otherwise silently
    coerce a non-string key (int/float/bool/None) into a string, which could
    make two structurally different payloads collide into the same JSON text
    (e.g. ``{1: "a"}`` and ``{"1": "a"}``). That coercion is exactly the kind
    of silent conversion this function exists to prevent, so non-string keys
    are rejected outright rather than accepted and coerced.
    """
    if value is None or isinstance(value, str):
        return
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ReviewPayloadFingerprintError(
                ReviewFingerprintErrorCode.NON_FINITE_FLOAT,
                f"{path}: NaN/Infinity cannot be canonically fingerprinted",
            )
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ReviewPayloadFingerprintError(
                    ReviewFingerprintErrorCode.NON_STRING_DICT_KEY,
                    f"{path}: dict keys must be str, got {type(key).__name__}",
                )
            _reject_json_unsafe_values(item, path=f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _reject_json_unsafe_values(item, path=f"{path}[{index}]")
        return
    raise ReviewPayloadFingerprintError(
        ReviewFingerprintErrorCode.UNSUPPORTED_TYPE,
        f"{path}: {type(value).__name__} is not a JSON-safe review payload value",
    )


def fingerprint_review_payload(payload: Any) -> str:
    """
    Canonical SHA-256 hex digest of an already-built review payload.

    ``payload`` must already be JSON-safe - the output of a later type-specific
    payload builder, not an arbitrary Python object. This function does not
    build payloads and does not know about Guide/Prompt/UseCase/Comparison
    field shapes; it only canonicalizes and hashes whatever it is given.

    Canonicalization: ``json.dumps`` with sorted keys (at every nesting level),
    the most compact separators, and ``ensure_ascii=False`` so Unicode
    characters are hashed as themselves rather than as escape sequences -
    encoded to UTF-8 before hashing, which keeps the digest a pure function of
    the payload's Unicode content, not of Python's or json's incidental
    formatting choices. List order is preserved (lists are not sorted - order
    is part of the reviewed content). Strings are hashed exactly as given: no
    trimming, no case-folding, no normalization.

    Raises :class:`ReviewPayloadFingerprintError` - never silently coerces -
    for anything that is not a JSON-safe value: model instances, querysets,
    sets, bytes, datetimes, ``Decimal``, ``UUID``, NaN/Infinity, non-string
    dict keys, or any other custom object. No database query, no cache access.
    """
    _reject_json_unsafe_values(payload, path="$")
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ======================================================================
# 2. Fingerprint syntax
# ======================================================================


_LOWERCASE_HEX_DIGITS = frozenset("0123456789abcdef")


def _is_valid_sha256_hexdigest(value: Any) -> bool:
    """
    True only for a 64-character string of lowercase hex digits - the exact
    shape :func:`fingerprint_review_payload` produces. Deliberately strict:
    uppercase, mixed case, surrounding whitespace, a ``0x`` prefix, hyphenated
    UUID shapes, and any length other than 64 are all rejected, because a
    fingerprint this permissive would compare unequal to itself under trivial
    reformatting and defeat the point of having a canonical form at all.
    """
    if not isinstance(value, str):
        return False
    if len(value) != 64:
        return False
    return all(char in _LOWERCASE_HEX_DIGITS for char in value)


# ======================================================================
# 3. Revision <-> root-object membership
# ======================================================================


def _resolve_db_alias(obj: Any, revision: Revision) -> str:
    """``using`` fallback chain: the object's own alias, then the revision's,
    then ``DEFAULT_DB_ALIAS`` - mirrors ``_state.db``'s "unset until saved or
    loaded" contract rather than assuming a router-computed alias."""
    obj_db = getattr(obj._state, "db", None)
    if obj_db:
        return obj_db
    revision_db = getattr(revision._state, "db", None)
    if revision_db:
        return revision_db
    return DEFAULT_DB_ALIAS


def revision_contains_object(revision: Revision | None, obj: Any, *, using: str | None = None) -> bool:
    """
    True only if ``revision`` contains an actual root :class:`~reversion.models.Version`
    of ``obj`` - not a child, not a translation, not a version of a different
    object or a different type.

    Confirms, in one targeted existence query against ``reversion.Version``
    joined to ``django_content_type``:

    * ``version.revision_id == revision.pk``,
    * ``version.content_type`` matches ``obj``'s *concrete* model's
      ``app_label``/``model`` - filtered through the public relation fields
      ``content_type__app_label`` and ``content_type__model`` rather than a
      pre-resolved ``ContentType`` row, so this never touches
      ``ContentType.objects.get_for_model()`` or its process-global cache.
      ``obj._meta.concrete_model`` is used explicitly (not ``type(obj)``) to
      reproduce Beta 11.11B1's active ``for_concrete_model=True`` registration
      option - the same rule ``ContentTypeManager._get_opts()`` applies
      internally before it would otherwise resolve a proxy model to its base,
    * ``version.object_id == str(obj.pk)``,
    * ``version.db`` matches the resolved database alias.

    Returns ``False`` - never raises - for every ordinary negative case:
    ``revision is None``, ``obj is None``, an unsaved object (``obj.pk is
    None``), an unsaved revision, a revision that does not contain this
    object, a version of a different object or a different content type, a
    version recorded under a different database alias, or a revision that has
    since been deleted (its versions cascade-delete with it, so the query
    simply finds nothing). A malformed ``using`` argument is a programming
    error and raises ``TypeError`` rather than degrading to ``False``.

    Performs exactly one query against ``reversion.Version`` regardless of
    ``ContentType`` cache state - a cold cache no longer costs a second,
    separate ``django_content_type`` lookup the way
    ``ContentType.objects.get_for_model()`` would. Never materializes the
    revision's full version set and never queries child/translation models.
    """
    if revision is None or obj is None:
        return False
    if obj.pk is None:
        return False
    if revision.pk is None:
        return False
    if using is not None and not isinstance(using, str):
        raise TypeError(f"using must be a database alias string, got {type(using).__name__}")

    db_alias = using if using is not None else _resolve_db_alias(obj, revision)
    model_meta = obj._meta.concrete_model._meta

    return (
        Version.objects.using(db_alias)
        .filter(
            revision_id=revision.pk,
            content_type__app_label=model_meta.app_label,
            content_type__model=model_meta.model_name,
            object_id=str(obj.pk),
            db=db_alias,
        )
        .exists()
    )


# ======================================================================
# 4/5/6. Structured binding failures and validation
# ======================================================================


class BindingFailureReason(StrEnum):
    """Stable, individually testable reasons a review/approval binding is
    invalid. Not a translated message - see the type's own module docstring
    for why: this slice introduces no admin-facing text."""

    REVIEW_REVISION_MISSING = "review_revision_missing"
    REVIEW_FINGERPRINT_MISSING = "review_fingerprint_missing"
    REVIEW_FINGERPRINT_INVALID = "review_fingerprint_invalid"
    REVIEW_REVISION_NOT_FOR_OBJECT = "review_revision_not_for_object"
    APPROVED_REVISION_MISSING = "approved_revision_missing"
    APPROVED_REVISION_MISMATCH = "approved_revision_mismatch"
    APPROVED_REVISION_NOT_FOR_OBJECT = "approved_revision_not_for_object"


@dataclass(frozen=True, slots=True)
class BindingValidationResult:
    """
    Immutable outcome of :func:`validate_review_binding` /
    :func:`validate_approved_binding`.

    Deliberately not a bare bool: a falsy result without a reason would force
    every caller to re-derive *why* it failed, and a naive "truthiness" read
    of this object is not offered - always check :attr:`is_valid` explicitly.

    Valid: ``is_valid is True`` and ``reason is None``.
    Invalid: ``is_valid is False`` and ``reason`` is exactly one
    :class:`BindingFailureReason`.
    """

    is_valid: bool
    reason: BindingFailureReason | None


def validate_review_binding(obj: Any, *, using: str | None = None) -> BindingValidationResult:
    """
    Checks only the *syntactic and relational* review binding - not whether
    the fingerprint matches the object's current content (no builder exists
    yet to compare against), not ``obj.status``, and not who is allowed to
    submit. Those are later slices' concerns.

    Checked in this fixed order, so a caller sees a deterministic reason when
    more than one thing is wrong at once:

    1. :data:`BindingFailureReason.REVIEW_REVISION_MISSING` - no revision set.
    2. :data:`BindingFailureReason.REVIEW_FINGERPRINT_MISSING` - empty
       fingerprint.
    3. :data:`BindingFailureReason.REVIEW_FINGERPRINT_INVALID` - set but not a
       64-character lowercase hex digest.
    4. :data:`BindingFailureReason.REVIEW_REVISION_NOT_FOR_OBJECT` - the
       revision exists but does not actually contain a root version of
       ``obj`` (see :func:`revision_contains_object`).

    Read-only: fetches ``obj.review_revision`` (a normal FK access) and runs
    the one targeted existence query described in
    :func:`revision_contains_object`. No mutation.
    """
    if obj.review_revision_id is None:
        return BindingValidationResult(
            is_valid=False, reason=BindingFailureReason.REVIEW_REVISION_MISSING
        )

    if not obj.review_payload_fingerprint:
        return BindingValidationResult(
            is_valid=False, reason=BindingFailureReason.REVIEW_FINGERPRINT_MISSING
        )

    if not _is_valid_sha256_hexdigest(obj.review_payload_fingerprint):
        return BindingValidationResult(
            is_valid=False, reason=BindingFailureReason.REVIEW_FINGERPRINT_INVALID
        )

    if not revision_contains_object(obj.review_revision, obj, using=using):
        return BindingValidationResult(
            is_valid=False, reason=BindingFailureReason.REVIEW_REVISION_NOT_FOR_OBJECT
        )

    return BindingValidationResult(is_valid=True, reason=None)


def validate_approved_binding(obj: Any, *, using: str | None = None) -> BindingValidationResult:
    """
    An approval is only meaningful on top of a valid review binding, so any
    review-binding failure is returned unchanged as the approval's failure
    too - checked first, in the fixed order below:

    1. Whatever :func:`validate_review_binding` returned, if invalid.
    2. :data:`BindingFailureReason.APPROVED_REVISION_MISSING` - no approved
       revision set.
    3. :data:`BindingFailureReason.APPROVED_REVISION_MISMATCH` -
       ``approved_revision_id != review_revision_id``.
    4. :data:`BindingFailureReason.APPROVED_REVISION_NOT_FOR_OBJECT` - the
       approved revision does not actually contain a root version of ``obj``.

    Step 4 runs its own :func:`revision_contains_object` check even when the
    two ids are already known to be equal (step 3 passed): the equality check
    alone would not catch an object whose row was somehow moved onto a
    revision that used to be valid but no longer contains a matching version,
    and inferring membership from the review check's earlier result would let
    exactly that kind of inconsistency pass unnoticed.

    Does not check ``obj.status``, the reviewer, ``reviewed_at``, or whether
    the approved payload still matches the object's current content - those
    remain out of scope for this slice. Read-only.
    """
    review_result = validate_review_binding(obj, using=using)
    if not review_result.is_valid:
        return review_result

    if obj.approved_revision_id is None:
        return BindingValidationResult(
            is_valid=False, reason=BindingFailureReason.APPROVED_REVISION_MISSING
        )

    if obj.approved_revision_id != obj.review_revision_id:
        return BindingValidationResult(
            is_valid=False, reason=BindingFailureReason.APPROVED_REVISION_MISMATCH
        )

    if not revision_contains_object(obj.approved_revision, obj, using=using):
        return BindingValidationResult(
            is_valid=False, reason=BindingFailureReason.APPROVED_REVISION_NOT_FOR_OBJECT
        )

    return BindingValidationResult(is_valid=True, reason=None)


# ======================================================================
# 7/8. Live-snapshot detection and the pure invalidation target
# ======================================================================


def has_provable_live_snapshot(obj: Any) -> bool:
    """
    The Beta 11.11B2A live-snapshot contract, as a pure function instead of
    migration-only code - so later slices (and this module's own
    :func:`target_status_after_review_invalidation`) can reuse the exact same
    rule instead of re-deriving it.

    * Guide, Prompt, UseCase: ``bool(obj.live_i18n)``.
    * Comparison: ``bool(obj.live_i18n) and obj.live_entries is not None`` -
      ``live_entries == []`` is a genuine published-with-zero-entries
      snapshot and counts; ``live_entries is None`` is the pre-Beta-11.9
      legacy state and does not.

    ``last_published_revision_id``, ``status`` and ``is_published`` are never
    consulted - matching the B2A contract exactly, not a broader "is this
    published" heuristic.

    Checked with explicit ``isinstance`` against exactly the four supported
    concrete types rather than duck-typing on ``hasattr(obj, "live_i18n")``:
    an unsupported model that happens to carry a same-named field must raise,
    not silently return ``False`` and mask a genuinely unknown live-state
    contract. No database query, no mutation.
    """
    if isinstance(obj, Comparison):
        return bool(obj.live_i18n) and obj.live_entries is not None
    if isinstance(obj, _SIMPLE_LIVE_SNAPSHOT_TYPES):
        return bool(obj.live_i18n)
    raise TypeError(
        f"has_provable_live_snapshot() has no live-snapshot contract for "
        f"{type(obj).__name__}; only Guide, Prompt, UseCase and Comparison "
        f"are supported."
    )


def target_status_after_review_invalidation(obj: Any) -> str:
    """
    The status a future invalidation mechanism would move ``obj`` to - purely
    computed, never applied. Does not read or validate ``obj.status``, does
    not consult ``last_published_revision_id``, and does not check permissions
    or transition legality; it answers exactly one question: is there a
    provable live snapshot to fall back to?

    * yes -> ``EditorialWorkflowMixin.STATUS_REWORK``.
    * no  -> ``EditorialWorkflowMixin.STATUS_DRAFT``.

    Mirrors the Beta 11.11B2A migration cleanup's own target-status decision
    (see ``guides/migrations/0009_guide_approved_revision_and_more.py`` and
    its sibling migrations), now available as a reusable pure function. Does
    not mutate ``obj``, create a revision, or emit a signal.
    """
    if has_provable_live_snapshot(obj):
        return EditorialWorkflowMixin.STATUS_REWORK
    return EditorialWorkflowMixin.STATUS_DRAFT
