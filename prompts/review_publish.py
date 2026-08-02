"""
Beta 11.11D2: atomic, fail-closed prompt publish.

``publish_prompt_review`` takes exactly one saved Prompt from ``approved`` to
``published`` and, in the same transaction and the same single reversion
revision, writes everything the public site reads: the per-language
``live_i18n`` snapshot, ``live_author``, each published translation's
``public_slug``, ``is_published``, ``published_at`` and finally
``last_published_revision_id``. Any failure rolls all of it back. There is no
partial publish and no ``published`` row without a complete live projection.

Why this replaces two divergent paths
--------------------------------------
Until D2 a prompt could be published two ways, and they did not agree:

* ``core.admin.EditorialWorkflowAdminMixin.action_publish`` wrapped a *whole
  changelist selection* in one shared ``transaction.atomic()`` and one shared
  ``reversion.create_revision()``, then called
  ``set_last_published_revision()``. That helper resolves the marker with
  ``Version.objects.get_for_object(obj).first()`` - and ``Version.Meta``
  declares no ``ordering`` at all, so ``.first()`` is not deterministic;
  worse, it runs *inside* the still-open revision block, before reversion has
  written the versions for it, so the marker could only ever point at some
  older version (typically the approval one).
* ``content.views.editorial``'s generic ``STATUS_TRANSITIONS`` dispatch called
  ``transition(by=...)`` plus ``obj.save()`` with no transaction of its own,
  no reversion context, no marker at all, and - decisively - no check that the
  approval binding or the approved fingerprint still described the content
  being published.

Neither validated the binding it was publishing, so both could produce a
``published`` row whose live projection did not match what anyone had actually
approved. That is what this module exists to make impossible.

What is checked, in this order, before anything changes
---------------------------------------------------------
On a freshly ``SELECT ... FOR UPDATE``-locked row - never on the caller's
possibly-stale instance:

1. the actor may publish *this* object (``content.publish``, evaluated through
   the existing ``rules`` predicates, never a hardcoded group name);
2. the row really is ``approved``;
3. ``review_revision`` is structurally valid (Beta 11.11B2B1
   ``validate_review_binding``);
4. ``approved_revision`` is structurally valid and equal to
   ``review_revision``, and that revision really contains this prompt's root
   version (``validate_approved_binding``);
5. the canonical Beta 11.11C1/C4D v2 payload of what is on disk *right now*
   still fingerprints to the stored approved value - so any content, tag, tool
   or author-FK change since approval blocks the publish (an author *rename*
   does not, by the payload-v2 contract);
6. at least one genuinely complete translation exists.

Public slug ordering
---------------------
The policy itself is unchanged and still lives in
``Prompt.on_after_publish()``: ``public_slug`` mirrors ``slug``. D2 only fixes
*when* it is applied. ``EditorialWorkflowMixin.publish()`` calls
``_update_live_snapshot()`` first and ``on_after_publish()`` second, so before
D2 the snapshot froze ``public_slug`` as it was *before* the policy ran -
typically ``None`` on a first publish - while the translation row got the new
value afterwards. Snapshot and database therefore disagreed (Beta 11.11C4J-R3
audit, section 8). This module applies the policy through Parler *before*
calling the FSM transition, so ``on_after_publish()``'s own loop finds nothing
left to do and both end up with the same value.

Reversion and the marker
-------------------------
Exactly one revision per publish, captured through the same call-local
``post_revision_commit`` receiver plus ``ContextVar`` token that Beta 11.11C2A
established (see ``prompts/review_submission.py`` for the full rationale:
never a "newest revision" heuristic, because a concurrent commit could win
that race). The root version is then resolved by an exact
``(revision, content_type, object_id, db)`` lookup - a combination
``reversion.Version`` has a ``unique_together`` on, so it is unique by
construction - and its id is stored as ``last_published_revision_id`` in a
second targeted save *outside* the closed reversion context but *inside* the
same outer transaction, so it creates no second revision.

Known legacy boundary: ``last_published_revision_id`` is a ``Version.id``, and
the marker is written after the root version has been serialized. The root
version's own ``serialized_data`` therefore still carries the *previous*
marker value. That self-reference is inherent to storing a pointer to a
version inside the row that version snapshots; D2 does not attempt to rewrite
serialized data to paper over it. The database row is what the runtime reads,
and it points at the correct, just-created root version.
"""
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import reversion
from django.contrib.auth import get_user_model
from django.db import DEFAULT_DB_ALIAS, connections, transaction
from reversion.models import Version
from reversion.signals import post_revision_commit

from core.review_binding import (
    BindingFailureReason,
    fingerprint_review_payload,
    validate_approved_binding,
    validate_review_binding,
)
from prompts.models import Prompt, PromptTranslation
from prompts.review_payload import build_prompt_review_payload

User = get_user_model()

#: The revision comment every prompt publish writes. Stable and English, in
#: the same spirit as C2A's ``submit_for_review`` and C3A's ``approve`` - no
#: request data, language or random marker ever enters the persisted comment.
PUBLISH_REVISION_COMMENT = "publish"

#: The one status a publish may start from. Deliberately identical to the FSM
#: ``publish`` transition's own ``source`` rather than a wider gate that
#: happens to be technically reachable.
_PUBLISHABLE_STATUS = Prompt.STATUS_APPROVED

#: Translation fields that must carry real content for a language to be
#: publishable. ``intro``/``body``/``outro`` are ``blank=True`` on the model
#: and are therefore genuinely optional; ``title`` and ``slug`` are not, and
#: are what every public surface needs to render and route the page. Checked
#: on the stored value rather than trusting the form layer, because a row can
#: reach the database through a migration, a shell or a direct update.
_REQUIRED_TRANSLATION_FIELDS = ("title", "slug")

#: Identifies the currently-running publish call within its execution context,
#: so the per-call ``post_revision_commit`` receiver captures only the revision
#: this call produced. Same mechanism as C2A - see its module docstring.
_active_publish_token: ContextVar[str | None] = ContextVar(
    "prompt_publish_token", default=None
)


class PromptReviewPublishErrorCode(StrEnum):
    """Stable reason :func:`publish_prompt_review` refused to run or aborted,
    independent of message wording."""

    UNSUPPORTED_OBJECT = "unsupported_object"
    UNSAVED_OBJECT = "unsaved_object"
    INVALID_DATABASE_ALIAS = "invalid_database_alias"
    DATABASE_ALIAS_MISMATCH = "database_alias_mismatch"
    OBJECT_NOT_FOUND = "object_not_found"
    INVALID_ACTOR = "invalid_actor"
    ACTOR_DATABASE_ALIAS_MISMATCH = "actor_database_alias_mismatch"
    PERMISSION_DENIED = "permission_denied"
    ACTIVE_REVERSION_CONTEXT = "active_reversion_context"
    STATUS_NOT_PUBLISHABLE = "status_not_publishable"
    REVIEW_BINDING_INVALID = "review_binding_invalid"
    APPROVED_BINDING_INVALID = "approved_binding_invalid"
    REVIEW_PAYLOAD_CHANGED = "review_payload_changed"
    NO_PUBLISHABLE_TRANSLATION = "no_publishable_translation"
    INCOMPLETE_TRANSLATION = "incomplete_translation"
    REVISION_NOT_CAPTURED = "revision_not_captured"
    MULTIPLE_REVISIONS_CAPTURED = "multiple_revisions_captured"
    ROOT_VERSION_MISSING = "root_version_missing"
    ROOT_VERSION_AMBIGUOUS = "root_version_ambiguous"
    PUBLISH_POSTCONDITION_FAILED = "publish_postcondition_failed"


class PromptReviewPublishError(ValueError):
    """
    Raised by :func:`publish_prompt_review` for every input, alias, actor,
    permission, status, binding, payload, translation, revision or
    postcondition problem it refuses to proceed past.

    Carries a stable :attr:`code`, mirroring the other ``review_*`` modules.
    For the two binding checks it also carries the central
    :class:`~core.review_binding.BindingFailureReason` that
    ``validate_review_binding``/``validate_approved_binding`` produced, so a
    caller never has to re-derive *why* a binding was rejected;
    :attr:`binding_reason` is ``None`` for every other code.
    """

    def __init__(
        self,
        code: PromptReviewPublishErrorCode,
        message: str,
        *,
        binding_reason: BindingFailureReason | None = None,
    ):
        self.code = code
        self.binding_reason = binding_reason
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class PromptReviewPublishResult:
    """
    Immutable outcome of a successful :func:`publish_prompt_review`.

    Carries only plain scalars and a tuple of language codes - never a
    ``Prompt``, ``Revision`` or ``Version`` instance, so nothing here can
    trigger a lazy query after the call returns.

    ``previous_status`` is always ``"approved"``; ``current_status`` is always
    ``"published"``; ``revision_id`` is the single revision this publish
    produced; ``root_version_id`` is that revision's root version of this
    prompt and equals the row's new ``last_published_revision_id``;
    ``published_language_codes`` lists exactly the languages written into
    ``live_i18n``, sorted; ``fingerprint`` is the (unchanged) approved
    ``review_payload_fingerprint``.
    """

    prompt_id: int
    database_alias: str
    previous_status: str
    current_status: str
    revision_id: int
    root_version_id: int
    published_language_codes: tuple[str, ...]
    fingerprint: str


def _resolve_database_alias(prompt: Prompt, using: str | None) -> str:
    """
    Alias resolution + validation, in the fixed order C1/C2A/C3A all use: an
    unknown alias name is always ``INVALID_DATABASE_ALIAS`` (checked before
    the mismatch check), so it is never misreported as a mere conflict with
    the object's own alias. No cross-database fallback, no hardcoded
    ``"default"``.
    """
    obj_db_alias = getattr(prompt._state, "db", None)

    if using is not None:
        if using not in connections:
            raise PromptReviewPublishError(
                PromptReviewPublishErrorCode.INVALID_DATABASE_ALIAS,
                f"{using!r} is not a configured database alias",
            )
        if obj_db_alias and obj_db_alias != using:
            raise PromptReviewPublishError(
                PromptReviewPublishErrorCode.DATABASE_ALIAS_MISMATCH,
                f"explicit using={using!r} does not match the object's own database "
                f"alias {obj_db_alias!r}",
            )
        return using

    db_alias = obj_db_alias or DEFAULT_DB_ALIAS
    if db_alias not in connections:
        raise PromptReviewPublishError(
            PromptReviewPublishErrorCode.INVALID_DATABASE_ALIAS,
            f"{db_alias!r} is not a configured database alias",
        )
    return db_alias


def _validate_actor(actor: Any, db_alias: str) -> None:
    """
    Publishing requires an actor: a *saved* instance of the real
    ``AUTH_USER_MODEL`` on a database alias that does not contradict the
    resolved publish alias. Mirrors C3A, which likewise requires one (unlike
    C2A's optional actor). This never reads ``request.user``; the caller
    passes the actor explicitly.
    """
    if actor is None or not isinstance(actor, User) or actor.pk is None:
        raise PromptReviewPublishError(
            PromptReviewPublishErrorCode.INVALID_ACTOR,
            "actor is required and must be a saved AUTH_USER_MODEL instance, got "
            f"{'None' if actor is None else type(actor).__name__}",
        )
    actor_db_alias = getattr(actor._state, "db", None)
    if actor_db_alias and actor_db_alias != db_alias:
        raise PromptReviewPublishError(
            PromptReviewPublishErrorCode.ACTOR_DATABASE_ALIAS_MISMATCH,
            f"actor is bound to database alias {actor_db_alias!r}, not the "
            f"publish alias {db_alias!r}",
        )


def _require_publish_permission(actor: Any, locked: Prompt) -> None:
    """
    Fail-closed object-level permission check through the existing
    ``core.authz`` rules (``content.publish`` = ``is_author | is_editor``).

    Deliberately re-checked here even though both callers already gate the
    action for UX reasons: the primitive is the security boundary, and a
    caller that forgets - or a future caller that does not exist yet - must
    not be able to publish. No new permission, no hardcoded group name.
    """
    if not actor.has_perm("content.publish", locked):
        raise PromptReviewPublishError(
            PromptReviewPublishErrorCode.PERMISSION_DENIED,
            f"actor #{actor.pk} is not allowed to publish prompts.Prompt #{locked.pk}",
        )


def _validate_bindings(locked: Prompt, db_alias: str) -> None:
    """
    Central Beta 11.11B2B1 binding validation - never reimplemented here.

    ``validate_approved_binding`` already re-runs the whole review check first
    and additionally proves ``approved_revision == review_revision`` and that
    the revision contains this root, so the two calls together cover every
    binding requirement D2 states. The review check is still made explicitly
    so a purely review-side defect reports ``REVIEW_BINDING_INVALID`` rather
    than being flattened into the approval code.
    """
    review_result = validate_review_binding(locked, using=db_alias)
    if not review_result.is_valid:
        raise PromptReviewPublishError(
            PromptReviewPublishErrorCode.REVIEW_BINDING_INVALID,
            f"prompt #{locked.pk} has no valid review binding: {review_result.reason}",
            binding_reason=review_result.reason,
        )

    approved_result = validate_approved_binding(locked, using=db_alias)
    if not approved_result.is_valid:
        raise PromptReviewPublishError(
            PromptReviewPublishErrorCode.APPROVED_BINDING_INVALID,
            f"prompt #{locked.pk} has no valid approval binding: "
            f"{approved_result.reason}",
            binding_reason=approved_result.reason,
        )


def _verify_approved_fingerprint(locked: Prompt, db_alias: str) -> str:
    """
    The stored approved fingerprint must still describe what is on disk right
    now. Uses only the canonical Beta 11.11C1/C4D builder and the central
    fingerprinter - no local payload assembly, no local hashing.

    By the payload-v2 contract this means a changed title, intro, body,
    outro, slug, public_slug, tag set, tool set or ``author_id`` blocks the
    publish, while a *rename* of the author's account does not.
    """
    stored_fingerprint = locked.review_payload_fingerprint
    payload = build_prompt_review_payload(locked, using=db_alias)
    current_fingerprint = fingerprint_review_payload(payload)
    if current_fingerprint != stored_fingerprint:
        raise PromptReviewPublishError(
            PromptReviewPublishErrorCode.REVIEW_PAYLOAD_CHANGED,
            f"prompt #{locked.pk}'s content changed since it was approved; it must "
            "be reviewed again before it can be published",
        )
    return stored_fingerprint


def _resolve_publishable_languages(locked: Prompt, db_alias: str) -> tuple[str, ...]:
    """
    Every language that genuinely has a complete stored translation, sorted.

    Reads the ``PromptTranslation`` rows directly rather than through Parler's
    ``safe_translation_getter``: that helper can silently fall back to another
    language, which is exactly what must never influence what gets published.
    A language is publishable only if its own row carries non-empty
    :data:`_REQUIRED_TRANSLATION_FIELDS`.

    Fail-closed in both directions: no translation at all is
    ``NO_PUBLISHABLE_TRANSLATION``, and a translation that exists but is
    incomplete is ``INCOMPLETE_TRANSLATION`` - never silently skipped, because
    skipping it would publish a prompt while quietly dropping a language an
    editor believes is part of it.
    """
    rows = list(
        PromptTranslation.objects.using(db_alias)
        .filter(master_id=locked.pk)
        .values("language_code", *_REQUIRED_TRANSLATION_FIELDS)
    )
    if not rows:
        raise PromptReviewPublishError(
            PromptReviewPublishErrorCode.NO_PUBLISHABLE_TRANSLATION,
            f"prompt #{locked.pk} has no translation to publish",
        )

    incomplete = sorted(
        row["language_code"]
        for row in rows
        if any(not (row[name] or "").strip() for name in _REQUIRED_TRANSLATION_FIELDS)
    )
    if incomplete:
        raise PromptReviewPublishError(
            PromptReviewPublishErrorCode.INCOMPLETE_TRANSLATION,
            f"prompt #{locked.pk} has incomplete translations for "
            f"{', '.join(incomplete)}; every published language needs a "
            f"non-empty {' and '.join(_REQUIRED_TRANSLATION_FIELDS)}",
        )

    return tuple(sorted(row["language_code"] for row in rows))


def _apply_public_slug_policy(locked: Prompt, language_codes: tuple[str, ...]) -> None:
    """
    Apply the existing public-slug policy *before* the live snapshot is built.

    The policy is unchanged and still defined by
    ``Prompt.on_after_publish()``: ``public_slug`` mirrors ``slug``, including
    on a republish whose draft slug changed. Applying it here - through
    Parler, so the same cached translation the snapshot builder will read is
    updated - is the whole fix: ``publish()`` runs ``_update_live_snapshot()``
    before ``on_after_publish()``, so before D2 the snapshot froze the *old*
    ``public_slug`` (``None`` on a first publish) while the row got the new
    one moments later. ``on_after_publish()``'s own loop then finds
    ``public_slug == slug`` and becomes a no-op, so the policy still lives in
    exactly one place.

    Writes through Parler rather than a raw ``QuerySet.update()`` on purpose:
    a raw update would leave Parler's translation cache stale, and
    ``_update_live_snapshot()`` reads through that cache.
    """
    for language_code in language_codes:
        locked.set_current_language(language_code)
        if locked.slug and locked.public_slug != locked.slug:
            locked.public_slug = locked.slug


def _capture_publish_revision(locked: Prompt, actor: Any, db_alias: str) -> Any:
    """
    Run the FSM publish inside exactly one reversion revision and return that
    revision.

    Uses C2A's call-local capture: a unique ``dispatch_uid`` plus a
    ``ContextVar`` token, so two concurrent publishes - whose receivers are
    both connected process-wide and both fire for either commit - cannot claim
    each other's revision. Both the receiver and the token are torn down in
    ``finally``, after success, after any exception, and whether the signal
    fired once, not at all, or more than once.

    ``atomic=False``: the caller's outer ``transaction.atomic()`` is the
    rollback boundary; a nested atomic would only add a savepoint.
    """
    call_token = uuid.uuid4().hex
    dispatch_uid = f"prompts.review_publish:{call_token}"
    captured: list = []

    def _capture_receiver(sender, revision, versions, **kwargs):
        if _active_publish_token.get() != call_token:
            return
        if not any(getattr(version, "db", None) == db_alias for version in versions):
            return
        captured.append(revision)

    variable_token = _active_publish_token.set(call_token)
    post_revision_commit.connect(_capture_receiver, dispatch_uid=dispatch_uid, weak=False)
    try:
        with reversion.create_revision(atomic=False, using=db_alias):
            reversion.set_user(actor)
            reversion.set_comment(PUBLISH_REVISION_COMMENT)

            # Real FSM transition - never a bare status assignment (`status`
            # is a protected FSMField). `publish()` itself writes `live_i18n`
            # via `_update_live_snapshot()` and then `is_published`,
            # `published_at` and `live_author` via `on_after_publish()`; no
            # note is passed, so `review_note` is left exactly as stored,
            # matching C2A/C3A.
            locked.publish(by=actor)

            # One explicit save persists the root fields `on_after_publish()`
            # set in memory plus, through Parler, the translations whose
            # `public_slug` the policy above touched.
            locked.save()
    finally:
        post_revision_commit.disconnect(_capture_receiver, dispatch_uid=dispatch_uid)
        _active_publish_token.reset(variable_token)

    if not captured:
        raise PromptReviewPublishError(
            PromptReviewPublishErrorCode.REVISION_NOT_CAPTURED,
            f"no revision was captured for the publish of prompt #{locked.pk}",
        )
    if len(captured) > 1:
        raise PromptReviewPublishError(
            PromptReviewPublishErrorCode.MULTIPLE_REVISIONS_CAPTURED,
            f"{len(captured)} revisions were captured for the publish of "
            f"prompt #{locked.pk}",
        )
    return captured[0]


def _resolve_root_version(revision: Any, locked: Prompt, db_alias: str) -> Version:
    """
    The one root ``Version`` of this prompt inside the just-created revision.

    An exact ``(revision, content_type, object_id, db)`` lookup - never a
    "newest version" heuristic and never an unordered ``.first()``, which is
    what ``core.admin.set_last_published_revision()`` still uses and why its
    marker was never dependable (``reversion.Version`` declares no ``Meta``
    ordering at all). ``Version`` has a ``unique_together`` on exactly this
    combination, so more than one row is structurally impossible; the
    ambiguity check exists so a future schema change could not turn that into
    a silently wrong marker.
    """
    model_meta = Prompt._meta.concrete_model._meta
    versions = list(
        Version.objects.using(db_alias).filter(
            revision_id=revision.pk,
            content_type__app_label=model_meta.app_label,
            content_type__model=model_meta.model_name,
            object_id=str(locked.pk),
            db=db_alias,
        )[:2]
    )
    if not versions:
        raise PromptReviewPublishError(
            PromptReviewPublishErrorCode.ROOT_VERSION_MISSING,
            f"revision #{revision.pk} contains no root version of prompt #{locked.pk}",
        )
    if len(versions) > 1:
        raise PromptReviewPublishError(
            PromptReviewPublishErrorCode.ROOT_VERSION_AMBIGUOUS,
            f"revision #{revision.pk} contains more than one root version of "
            f"prompt #{locked.pk}",
        )
    return versions[0]


def _store_publish_marker(locked: Prompt, root_version: Version) -> None:
    """
    Point ``last_published_revision_id`` at the root version of the revision
    this publish just created.

    Runs *outside* the now-closed reversion context - so it records no second
    revision - but *inside* the same outer transaction, so a later failure
    still rolls it back. Exactly the technique C2A uses to bind
    ``review_revision`` after its own capture. ``updated_at`` is deliberately
    not re-touched.
    """
    locked.last_published_revision_id = root_version.pk
    locked.save(update_fields=["last_published_revision_id"])


def _verify_publish_postconditions(
    locked: Prompt,
    *,
    db_alias: str,
    language_codes: tuple[str, ...],
    fingerprint: str,
    root_version: Version,
) -> None:
    """
    Re-read the row from the database and prove the publish actually produced
    a complete, self-consistent live projection. Anything short of that raises
    :data:`PromptReviewPublishErrorCode.PUBLISH_POSTCONDITION_FAILED` and the
    caller's outer transaction rolls the whole publish back - no revision, no
    marker, no half-written snapshot.

    Deliberately checked against a fresh read rather than the in-memory
    instance: the point is to catch a write that silently did not land, which
    an in-memory assertion could never see. ``_update_live_snapshot()``
    swallows its own save errors (``except Exception: pass``), so this is the
    check that turns such a failure back into a hard error.
    """
    fresh = Prompt._default_manager.using(db_alias).get(pk=locked.pk)

    def fail(problem: str) -> None:
        raise PromptReviewPublishError(
            PromptReviewPublishErrorCode.PUBLISH_POSTCONDITION_FAILED,
            f"prompt #{locked.pk} publish postcondition failed: {problem}",
        )

    if fresh.status != Prompt.STATUS_PUBLISHED:
        fail(f"status is {fresh.status!r}, expected 'published'")
    if fresh.is_published is not True:
        fail("is_published is not True")
    if fresh.published_at is None:
        fail("published_at is not set")
    if not fresh.live_i18n:
        fail("live_i18n is empty")
    if not isinstance(fresh.live_author, dict) or "display_name" not in fresh.live_author:
        fail("live_author is not a well-formed publish snapshot")
    if fresh.review_payload_fingerprint != fingerprint:
        fail("the approved fingerprint was modified during publish")
    if fresh.last_published_revision_id != root_version.pk:
        fail("last_published_revision_id does not point at this publish's root version")

    if set(fresh.live_i18n) != set(language_codes):
        fail(
            f"live_i18n covers {sorted(fresh.live_i18n)!r}, expected "
            f"{sorted(language_codes)!r}"
        )

    stored = {
        row["language_code"]: row
        for row in PromptTranslation.objects.using(db_alias)
        .filter(master_id=fresh.pk)
        .values("language_code", "title", "intro", "body", "outro", "slug", "public_slug")
    }
    for language_code in language_codes:
        snapshot = fresh.live_i18n.get(language_code)
        if not isinstance(snapshot, dict):
            fail(f"live_i18n[{language_code!r}] is not a snapshot dict")
        row = stored.get(language_code)
        if row is None:
            fail(f"translation for {language_code!r} disappeared during publish")
        for field in Prompt.LIVE_SNAPSHOT_FIELDS:
            if snapshot.get(field) != row[field]:
                fail(
                    f"live_i18n[{language_code!r}][{field!r}] does not match the "
                    "published translation"
                )
        if not (snapshot.get("title") or "").strip():
            fail(f"live_i18n[{language_code!r}] has no title")


def publish_prompt_review(
    prompt: Any, *, actor: Any, using: str | None = None
) -> PromptReviewPublishResult:
    """
    Atomically publish exactly one saved Prompt that is currently ``approved``
    with a valid, unchanged approval binding; see the module docstring for the
    full contract.

    Accepts only a saved ``prompts.Prompt`` (or a proxy resolving to it);
    ``actor`` is a required saved user, checked against ``content.publish`` on
    this exact object and recorded as the revision's author. Returns a
    :class:`PromptReviewPublishResult` on success and raises
    :class:`PromptReviewPublishError` (with a stable
    :class:`PromptReviewPublishErrorCode`) on every refusal or abort - never a
    bare no-op, and never a swallowed exception. Programming, database,
    integrity and reversion-infrastructure errors propagate unchanged.
    """
    # 1-2. Object type and primary key.
    if prompt is None or not isinstance(prompt, Prompt):
        raise PromptReviewPublishError(
            PromptReviewPublishErrorCode.UNSUPPORTED_OBJECT,
            "publish_prompt_review() only accepts a prompts.Prompt instance, got "
            f"{'None' if prompt is None else type(prompt).__name__}",
        )
    if prompt.pk is None:
        raise PromptReviewPublishError(
            PromptReviewPublishErrorCode.UNSAVED_OBJECT,
            "publish_prompt_review() requires a saved object with a primary key",
        )

    # 3-5. Database alias (type check, then existence, then mismatch).
    if using is not None and not isinstance(using, str):
        raise TypeError(f"using must be a database alias string, got {type(using).__name__}")
    db_alias = _resolve_database_alias(prompt, using)

    # 6. Actor - required, like C3A.
    _validate_actor(actor, db_alias)

    # 7. Reject an already-active reversion context: this publish must own its
    # revision graph, never a nested or shared one. Before any query or
    # mutation - this is what makes the admin changelist bypass necessary.
    if reversion.is_active():
        raise PromptReviewPublishError(
            PromptReviewPublishErrorCode.ACTIVE_REVERSION_CONTEXT,
            "publish_prompt_review() must not run inside an active "
            "reversion.create_revision() block",
        )

    # 8. Outer transaction - the sole rollback boundary for everything below.
    with transaction.atomic(using=db_alias):
        # 9. Fresh, locked root through the unfiltered manager. The caller's
        # instance is used only for its model, pk and alias.
        try:
            locked = (
                Prompt._default_manager.using(db_alias)
                .select_for_update()
                # `prefetch_related` is not an optimisation here, it is a
                # correctness requirement. Parler resolves a translation from
                # its local cache, then from prefetched rows, and only then
                # from Django's *shared* cache - so any write that bypassed
                # Parler (a reversion `revert()`, a data migration, a raw
                # `QuerySet.update()`, a shell fix) leaves a stale translation
                # in that shared cache which even a freshly constructed
                # instance still reads. `_update_live_snapshot()` builds
                # `live_i18n` through exactly that path, so without this the
                # publish could freeze stale content while the fingerprint -
                # which queries the database directly - happily matched.
                # Prefetching puts real database rows in front of the shared
                # cache and refreshes it on the way.
                .prefetch_related("translations")
                .get(pk=prompt.pk)
            )
        except Prompt.DoesNotExist:
            raise PromptReviewPublishError(
                PromptReviewPublishErrorCode.OBJECT_NOT_FOUND,
                f"prompts.Prompt #{prompt.pk} no longer exists",
            ) from None

        # 10. Permission, on the locked row.
        _require_publish_permission(actor, locked)

        # 11. Publishability decided on the locked database row, never the
        # caller's possibly-stale copy.
        previous_status = locked.status
        if previous_status != _PUBLISHABLE_STATUS:
            raise PromptReviewPublishError(
                PromptReviewPublishErrorCode.STATUS_NOT_PUBLISHABLE,
                f"prompt #{locked.pk} is in status {previous_status!r}, which cannot "
                "be published",
            )

        # 12-13. Central binding validation, then the payload/fingerprint
        # re-check against what is actually on disk right now.
        _validate_bindings(locked, db_alias)
        fingerprint = _verify_approved_fingerprint(locked, db_alias)

        # 14-15. Which languages go live, and the public-slug policy applied
        # before the snapshot builder reads them.
        language_codes = _resolve_publishable_languages(locked, db_alias)
        _apply_public_slug_policy(locked, language_codes)

        # 16. Exactly one reversion revision around the FSM publish and saves.
        revision = _capture_publish_revision(locked, actor, db_alias)

        # 17. The exact root version of that revision, then the marker.
        root_version = _resolve_root_version(revision, locked, db_alias)
        _store_publish_marker(locked, root_version)

        # 18. Prove the live projection is complete and self-consistent.
        _verify_publish_postconditions(
            locked,
            db_alias=db_alias,
            language_codes=language_codes,
            fingerprint=fingerprint,
            root_version=root_version,
        )

        # 19-20. Structured result; the outer transaction commits on exit.
        return PromptReviewPublishResult(
            prompt_id=locked.pk,
            database_alias=db_alias,
            previous_status=previous_status,
            current_status=locked.status,
            revision_id=revision.pk,
            root_version_id=root_version.pk,
            published_language_codes=language_codes,
            fingerprint=fingerprint,
        )
