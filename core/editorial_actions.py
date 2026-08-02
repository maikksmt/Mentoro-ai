"""
Beta 11.13D1B: the one place an editorial workflow action is actually executed.

Why this module exists
----------------------
Until D1B the same business action ran through two different implementations.
``core.admin.EditorialWorkflowAdminMixin.action_*`` wrapped the FSM transition
in a ``reversion.create_revision()`` with a user and an audit comment, while
``content.views.editorial``'s generic dispatch called
``getattr(obj, transition)(by=user)`` followed by ``obj.save()`` - no
transaction of its own, no revision, no revision user, no comment and no
publish marker. Beta 11.13D1A measured the result: for Guide, Use Case and
Comparison *every* workflow change made from the editorial workspace was
invisible to the revision history and left no rollback point, and the same was
true for a Prompt's rework/archive/restore.

:func:`apply_editorial_action` is now that single implementation. Both surfaces
delegate to it, so "the same action" is the same code rather than two paths
that happen to agree on a status string.

What stays outside this module
------------------------------
Permissions, messages, redirects, templates, request parsing and URL handling
all remain the responsibility of the calling surface, exactly as they are
today. This function assumes the caller has already established that the actor
may perform this action on this object - both call sites check the ``content.*``
``rules`` permission first, and neither the admin's nor the workspace's
permission model is touched by this slice. It is not a permission boundary and
must never be exposed directly to a request.

What it does guarantee, regardless of caller
--------------------------------------------
* the FSM precondition is re-checked (``can_proceed``) and refused fail-closed;
* the mutation is atomic;
* exactly one reversion revision covers the mutation, carrying the acting user
  and the canonical audit comment for that action;
* a publish points ``last_published_revision_id`` at the root ``Version`` of
  the revision the publish itself produced.

Nesting: joining vs. opening a revision
---------------------------------------
``reversion.create_revision()`` joins an already-active revision instead of
starting a second one, and the innermost ``set_user``/``set_comment`` wins for
the whole revision (``reversion.revisions._pop_frame``). That is deliberately
relied on here, because the two surfaces need different granularity:

* the **admin** dispatches actions inside ``VersionAdmin.changelist_view``'s
  revision *and* inside the action's own one, so a changelist selection keeps
  landing in exactly **one shared revision** - the pre-D1B bulk contract, left
  intact;
* the **workspace** mutates a single object with no revision active, so this
  module opens one.

No second, wrapping revision is ever created, and nothing here is nested inside
another revision of its own making.

The publish marker, and why it cannot be resolved inline
--------------------------------------------------------
``last_published_revision_id`` holds a ``reversion.Version.id`` - a legacy
name kept verbatim, see ``EditorialWorkflowMixin``. Pointing it at the publish
is only possible *after* reversion has written that revision's versions, which
happens when the **outermost** revision context exits. Inside the admin that is
after the action has already returned.

``core.admin.set_last_published_revision()`` ignored this: it ran inside the
still-open revision block and resolved the marker with an unordered
``Version.objects.get_for_object(obj).first()``. Measured on a clean database,
an admin publish therefore left the marker at ``None`` on a first publish and
pointed it at the *approval* revision otherwise - the defect
``prompts/review_publish.py`` already documented for the prompt path and closed
there.

So the marker is written from a ``post_revision_commit`` receiver instead:
reversion sends that signal from inside ``_save_revision``, i.e. still inside
the revision's transaction, once the versions exist. The receiver resolves the
root version by an exact ``(revision, content_type, object_id, db)`` lookup -
a combination ``Version`` has a ``unique_together`` on - and never by a
"newest version" heuristic. A failure there propagates, so the surrounding
transaction rolls back and no surface can report a publish that did not fully
happen.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum

import reversion
from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.db import DEFAULT_DB_ALIAS, transaction
from django_fsm import can_proceed
from reversion.models import Version
from reversion.signals import post_revision_commit

from core.models.editorial import EditorialWorkflowMixin


class EditorialAction(StrEnum):
    """The six workflow actions both surfaces offer. Nothing else is executable
    through this module - there is no generic "run any transition" escape."""

    SUBMIT_FOR_REVIEW = "submit_for_review"
    REQUEST_REWORK = "request_rework"
    APPROVE = "approve"
    PUBLISH = "publish"
    ARCHIVE = "archive"
    RESTORE_TO_DRAFT = "restore_to_draft"


class EditorialActionErrorCode(StrEnum):
    """Stable reason the action was refused or aborted, independent of wording."""

    UNSUPPORTED_ACTION = "unsupported_action"
    UNSUPPORTED_OBJECT = "unsupported_object"
    UNSAVED_OBJECT = "unsaved_object"
    INVALID_ACTOR = "invalid_actor"
    TRANSITION_UNAVAILABLE = "transition_unavailable"
    STATUS_NOT_ELIGIBLE = "status_not_eligible"
    MARKER_SCOPE_MISSING = "marker_scope_missing"
    ROOT_VERSION_MISSING = "root_version_missing"
    ROOT_VERSION_AMBIGUOUS = "root_version_ambiguous"


class EditorialActionError(ValueError):
    """Raised for every input, status or revision problem this module refuses
    to proceed past. Carries a stable :attr:`code`, mirroring the ``review_*``
    prompt modules."""

    def __init__(self, code: EditorialActionErrorCode, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class _ActionSpec:
    """The persistence contract of one action, reproduced from the admin.

    ``comment`` and ``default_note`` are the values
    ``EditorialWorkflowAdminMixin`` already wrote before D1B and are kept
    verbatim, so the audit history reads the same before and after this slice
    and both surfaces now agree on them.
    """

    #: name of the ``django_fsm`` transition on ``EditorialWorkflowMixin``
    transition: str
    #: the canonical reversion audit comment
    comment: str
    #: note handed to the transition when the caller passes none
    default_note: str
    #: fields to persist; ``None`` means a full ``save()``, which publish needs
    #: so Parler writes the translations whose ``public_slug`` changed
    save_fields: tuple[str, ...] | None
    #: archive/restore explicitly clear the publication flag, as the admin does
    clears_is_published: bool
    #: only publish resolves ``last_published_revision_id``
    writes_publish_marker: bool


_ACTION_SPECS: dict[EditorialAction, _ActionSpec] = {
    EditorialAction.SUBMIT_FOR_REVIEW: _ActionSpec(
        transition="move_to_review",
        comment="submit_for_review",
        default_note="Admin-Action: submit_for_review",
        # `review_note` is deliberately absent: the admin passed the note to
        # the transition but never persisted it, so the note above stays an
        # in-memory no-op. Reproduced rather than "cleaned up", because
        # persisting it now would start overwriting real editorial notes.
        save_fields=("status", "submitted_for_review_at", "updated_at"),
        clears_is_published=False,
        writes_publish_marker=False,
    ),
    EditorialAction.REQUEST_REWORK: _ActionSpec(
        transition="request_rework",
        comment="request_rework",
        default_note="",
        save_fields=("status", "review_note", "reviewed_at", "reviewed_by", "updated_at"),
        clears_is_published=False,
        writes_publish_marker=False,
    ),
    EditorialAction.APPROVE: _ActionSpec(
        transition="approve",
        comment="approve",
        default_note="",
        save_fields=("status", "review_note", "reviewed_at", "reviewed_by", "updated_at"),
        clears_is_published=False,
        writes_publish_marker=False,
    ),
    EditorialAction.PUBLISH: _ActionSpec(
        transition="publish",
        comment="Admin-Action: publish",
        default_note="Admin-Action publish",
        save_fields=None,
        clears_is_published=False,
        writes_publish_marker=True,
    ),
    EditorialAction.ARCHIVE: _ActionSpec(
        transition="archive",
        comment="archive",
        default_note="",
        save_fields=("status", "review_note", "is_published", "updated_at"),
        clears_is_published=True,
        writes_publish_marker=False,
    ),
    EditorialAction.RESTORE_TO_DRAFT: _ActionSpec(
        transition="restore",
        comment="restore",
        default_note="",
        save_fields=("status", "review_note", "is_published", "updated_at"),
        clears_is_published=True,
        writes_publish_marker=False,
    ),
}

#: Prompt owns dedicated, fail-closed primitives for these three actions
#: (Beta 11.11C2A/C3A/D2). They validate review and approval bindings, rebuild
#: the canonical payload and verify its fingerprint - none of which this
#: generic path knows about. Routing a Prompt through here for one of them
#: would silently downgrade it to an unbound FSM transition, so it is refused.
_PROMPT_ONLY_ACTIONS = frozenset(
    {
        EditorialAction.SUBMIT_FOR_REVIEW,
        EditorialAction.APPROVE,
        EditorialAction.PUBLISH,
    }
)


@dataclass(frozen=True, slots=True)
class _PendingPublishMarker:
    """One publish waiting for its revision to commit."""

    app_label: str
    model_name: str
    object_id: str
    db_alias: str


#: Publishes whose revision has not committed yet, within the *currently open
#: marker scope*. ``None`` means "no scope is open", which is the only state
#: this module treats as legal for code that is not publishing.
#:
#: Beta 11.13D1B1: the default is immutable on purpose. A mutable default would
#: be created once per process and then shared by every execution context that
#: never opened a scope - precisely the unbounded, process-wide collection
#: :func:`publish_marker_scope` exists to make impossible. Every scope installs
#: its *own* list and restores the previous value through the ``ContextVar``
#: token, so two independent actions can never observe each other's entries.
_pending_publish_markers: ContextVar[list[_PendingPublishMarker] | None] = ContextVar(
    "editorial_pending_publish_markers", default=None
)

#: Stable, namespaced identity of the module-level receiver below. Django
#: deduplicates receivers by ``dispatch_uid``, so re-importing or reloading
#: this module re-runs ``connect()`` without ever registering a second one.
PUBLISH_MARKER_RECEIVER_UID = "core.editorial_actions.publish_marker"


@contextmanager
def publish_marker_scope():
    """
    Bound the lifetime of pending publish markers to one editorial operation.

    Must enclose the **outermost** reversion revision of that operation,
    because ``post_revision_commit`` - the only thing that consumes an entry -
    is sent when that outermost context exits:

    * the workspace publishes a single object and
      :func:`apply_editorial_action` opens both the revision and this scope;
    * the admin's changelist runs inside ``VersionAdmin.changelist_view``'s
      revision, so ``EditorialWorkflowAdminMixin.changelist_view`` opens the
      scope around it - one scope for the whole bulk selection, matching the
      one shared revision that selection produces.

    The ``finally`` branch is the actual guarantee. Before D1B1 the entries
    were only ever removed by the signal, so any path that aborted the outer
    revision *before* it committed - a second bulk object raising, a failure in
    the receiver itself - left the first object's entry behind in a
    ``ContextVar`` that outlives the request in a threaded worker. A later,
    unrelated revision in the same thread would then have inherited it. Now
    every exit route - success, exception, or an abort that never reaches the
    signal - clears the collection and restores the previous value.

    Nesting is safe: each scope installs its own list and resets to exactly the
    value it replaced, never to ``None`` by assumption.
    """
    token = _pending_publish_markers.set([])
    try:
        yield
    finally:
        collection = _pending_publish_markers.get()
        if collection is not None:
            collection.clear()
        _pending_publish_markers.reset(token)


def _database_alias(obj) -> str:
    return obj._state.db or DEFAULT_DB_ALIAS


def _resolve_root_version(revision, entry: _PendingPublishMarker) -> Version | None:
    """
    The root ``Version`` of ``entry``'s object inside ``revision``.

    Exact lookup on ``(revision, content_type, object_id, db)`` - the tuple
    ``reversion.Version`` declares ``unique_together`` on - so more than one
    row is structurally impossible. Returns ``None`` when this revision does
    not contain the object at all, which is how the receiver distinguishes
    "not my revision" from "broken".
    """
    content_type = ContentType.objects.db_manager(entry.db_alias).get_by_natural_key(
        entry.app_label, entry.model_name
    )
    versions = list(
        Version.objects.using(entry.db_alias).filter(
            revision_id=revision.pk,
            content_type=content_type,
            object_id=entry.object_id,
            db=entry.db_alias,
        )[:2]
    )
    if not versions:
        return None
    if len(versions) > 1:
        raise EditorialActionError(
            EditorialActionErrorCode.ROOT_VERSION_AMBIGUOUS,
            f"revision #{revision.pk} contains more than one root version of "
            f"{entry.app_label}.{entry.model_name} #{entry.object_id}",
        )
    return versions[0]


def _write_pending_publish_markers(sender, revision, versions, **kwargs):
    """
    Point every pending publish at its root version, once the revision exists.

    Sent by reversion from inside ``_save_revision``, i.e. still inside the
    revision's transaction, so the marker write shares the fate of the publish
    it describes. Anything raised here propagates and rolls the whole mutation
    back rather than leaving a published row with a missing or wrong marker.

    Every entry in the scope belongs to *this* revision: a scope encloses
    exactly one outermost revision, and nested contexts join it rather than
    committing separately. An entry this revision does not contain is
    therefore not "not mine yet" but a broken assumption, and is refused
    rather than carried forward into some later commit (Beta 11.13D1B1 - that
    carry-forward was the opportunistic cleanup this slice removes).
    """
    pending = _pending_publish_markers.get()
    if not pending:
        return

    # Take the entries out of the collection first, so an exception below can
    # never leave a half-processed list behind for a later commit to inherit.
    entries = list(pending)
    pending.clear()

    unresolved: list[_PendingPublishMarker] = []
    for entry in entries:
        root_version = _resolve_root_version(revision, entry)
        if root_version is None:
            unresolved.append(entry)
            continue

        model = apps.get_model(entry.app_label, entry.model_name)
        # Targeted update rather than `instance.save()`: it must not re-enter
        # reversion's post_save receiver and add a second version of this row
        # to the very revision being committed.
        model.objects.using(entry.db_alias).filter(pk=entry.object_id).update(
            last_published_revision_id=root_version.pk
        )

    if unresolved:
        described = ", ".join(
            f"{e.app_label}.{e.model_name}#{e.object_id}" for e in unresolved
        )
        raise EditorialActionError(
            EditorialActionErrorCode.ROOT_VERSION_MISSING,
            f"revision #{revision.pk} contains no root version for pending "
            f"publish marker(s): {described}",
        )


post_revision_commit.connect(
    _write_pending_publish_markers,
    dispatch_uid=PUBLISH_MARKER_RECEIVER_UID,
    weak=False,
)


def _validate(obj, action: EditorialAction, actor) -> _ActionSpec:
    spec = _ACTION_SPECS.get(action)
    if spec is None:
        raise EditorialActionError(
            EditorialActionErrorCode.UNSUPPORTED_ACTION,
            f"{action!r} is not an editorial workflow action",
        )
    if not isinstance(obj, EditorialWorkflowMixin):
        raise EditorialActionError(
            EditorialActionErrorCode.UNSUPPORTED_OBJECT,
            f"{type(obj).__name__} does not use the editorial workflow",
        )
    if obj.pk is None:
        raise EditorialActionError(
            EditorialActionErrorCode.UNSAVED_OBJECT,
            "an unsaved object has no editorial workflow state to change",
        )
    if actor is None or not getattr(actor, "is_authenticated", False):
        raise EditorialActionError(
            EditorialActionErrorCode.INVALID_ACTOR,
            "an editorial workflow action needs an authenticated actor",
        )

    from prompts.models import Prompt

    if obj._meta.concrete_model is Prompt and action in _PROMPT_ONLY_ACTIONS:
        raise EditorialActionError(
            EditorialActionErrorCode.UNSUPPORTED_OBJECT,
            f"prompt #{obj.pk}: {action.value} must go through the sanctioned "
            f"prompts.review_* primitive, not the generic editorial action",
        )
    return spec


def apply_editorial_action(obj, action: EditorialAction, *, actor, note: str | None = None) -> None:
    """
    Run one editorial workflow action on one saved object.

    ``note`` defaults to the action's canonical note; callers that collect one
    from a form (the admin's rework/approve/archive/restore actions) pass it
    through. The object is mutated in place and left refreshed in memory.

    Raises :class:`EditorialActionError` with
    :attr:`~EditorialActionErrorCode.STATUS_NOT_ELIGIBLE` when the FSM refuses
    the transition from the current state - the routine "wrong state" outcome
    both surfaces already render their own message for. Everything else that
    goes wrong propagates unchanged; nothing is swallowed here.

    A publish additionally needs an open :func:`publish_marker_scope`. When no
    revision is active this call owns the revision and opens that scope itself;
    when one is already active the caller owns both, and a missing scope is
    refused with
    :attr:`~EditorialActionErrorCode.MARKER_SCOPE_MISSING` rather than falling
    back to a process-wide collection.
    """
    spec = _validate(obj, action, actor)
    db_alias = _database_alias(obj)

    transition = getattr(obj, spec.transition, None)
    if transition is None:
        raise EditorialActionError(
            EditorialActionErrorCode.TRANSITION_UNAVAILABLE,
            f"{type(obj).__name__} has no {spec.transition!r} transition",
        )
    if not can_proceed(transition):
        raise EditorialActionError(
            EditorialActionErrorCode.STATUS_NOT_ELIGIBLE,
            f"{type(obj).__name__} #{obj.pk} cannot {action.value} from "
            f"status {obj.status!r}",
        )

    entry: _PendingPublishMarker | None = None
    if spec.writes_publish_marker:
        meta = obj._meta.concrete_model._meta
        entry = _PendingPublishMarker(
            app_label=meta.app_label,
            model_name=meta.model_name,
            object_id=str(obj.pk),
            db_alias=db_alias,
        )

    if entry is not None and not reversion.is_active():
        # No revision is open, so this call owns the outermost one and must own
        # the marker scope around it too - the signal that consumes the entry
        # fires when that revision exits, i.e. inside this scope.
        with publish_marker_scope():
            _execute(obj, spec, transition, actor=actor, note=note,
                     db_alias=db_alias, entry=entry)
        return

    _execute(obj, spec, transition, actor=actor, note=note,
             db_alias=db_alias, entry=entry)


def _execute(obj, spec: _ActionSpec, transition, *, actor, note, db_alias, entry) -> None:
    """Run the mutation itself. Any marker scope it needs is already open."""
    with transaction.atomic(using=db_alias):
        if entry is not None:
            _register_pending_marker(entry)

        mutated = False
        try:
            # `atomic=False`: the block above is the rollback boundary. A
            # nested atomic would only add a savepoint.
            with reversion.create_revision(atomic=False, using=db_alias):
                reversion.set_user(actor)
                reversion.set_comment(spec.comment)

                transition(by=actor, note=note if note is not None else spec.default_note)
                if spec.clears_is_published:
                    obj.is_published = False
                _persist(obj, spec.save_fields)

            outermost = not reversion.is_active()
            mutated = True
        finally:
            # This object's own mutation failed and was rolled back to this
            # savepoint, but reversion keeps the version it collected in the
            # still-open outer frame. Leaving the entry queued would therefore
            # mark a row whose publish never happened once the shared revision
            # commits - the admin's bulk actions deliberately catch a single
            # object's failure and carry on with the rest.
            if entry is not None and not mutated:
                _discard_pending_marker(entry)

        if entry is not None and outermost:
            # We owned the revision, so it has already committed and the
            # receiver has already run. Anything still pending means the
            # marker was never written - never report that as a publish.
            _discard_pending_marker(entry)
            obj.refresh_from_db(fields=["last_published_revision_id"])
            if obj.last_published_revision_id is None:
                raise EditorialActionError(
                    EditorialActionErrorCode.ROOT_VERSION_MISSING,
                    f"{type(obj).__name__} #{obj.pk}: publish recorded no root "
                    f"version, so last_published_revision_id stayed unset",
                )


def _persist(obj, save_fields: tuple[str, ...] | None) -> None:
    """Save exactly what the action's contract says, mirroring the admin's
    ``_save_with_fields`` (which also tolerated a field a model does not
    declare)."""
    if save_fields is None:
        obj.save()
        return
    present = [name for name in save_fields if hasattr(obj, name)]
    if present:
        obj.save(update_fields=present)
    else:  # pragma: no cover - every editorial root declares all of them
        obj.save()


def _register_pending_marker(entry: _PendingPublishMarker) -> None:
    """
    Queue one publish for marking, fail-closed.

    A missing scope is refused rather than silently creating a collection: an
    implicitly created one would live in the ``ContextVar`` for the rest of the
    worker's life with nothing responsible for ending it, which is the whole
    failure mode :func:`publish_marker_scope` exists to prevent.
    """
    pending = _pending_publish_markers.get()
    if pending is None:
        raise EditorialActionError(
            EditorialActionErrorCode.MARKER_SCOPE_MISSING,
            f"publishing {entry.app_label}.{entry.model_name} #{entry.object_id} "
            f"requires an open publish_marker_scope() around the outermost "
            f"reversion revision",
        )
    pending.append(entry)


def _discard_pending_marker(entry: _PendingPublishMarker) -> None:
    pending = _pending_publish_markers.get()
    if pending and entry in pending:
        pending.remove(entry)
