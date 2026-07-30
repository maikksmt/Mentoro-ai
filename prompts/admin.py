# prompts/admin.py
from dataclasses import dataclass
from enum import StrEnum

from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin.utils import unquote
from django.db import DEFAULT_DB_ALIAS, connections, transaction
from django.http import Http404, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, render
from django.template.response import TemplateResponse
from django.urls import path
from django.utils import translation
from django.utils.formats import date_format
from django.utils.translation import (
    gettext_lazy as _,
    get_language,
    get_language_info,
    ngettext,
)
from parler.utils.context import switch_language
from reversion.admin import VersionAdmin
from reversion.models import Revision, Version

from content.templatetags.richtext import richtext
from core.admin import TranslatableTinyMCEMixin, EditorialWorkflowAdminMixin
from core.editorial_preview import (
    apply_editorial_preview_headers,
    has_saved_translation,
    is_supported_preview_language,
)
from core.models.editorial import EditorialWorkflowMixin
from core.review_binding import (
    ReviewInvalidationResult,
    fingerprint_review_payload,
    invalidate_editorial_review_state,
    revision_contains_object,
    validate_approved_binding,
    validate_review_binding,
)
from core.services import get_live_display_instance, build_field_diffs
from .models import Prompt, PromptTranslation
from .presentation import build_draft_prompt_context
from .review_approval import (
    PromptReviewApprovalError,
    PromptReviewApprovalErrorCode,
    approve_prompt_review,
)
from .review_edit_guard import (
    PromptReviewEditBaseline,
    capture_prompt_review_edit_baseline,
    invalidate_prompt_review_if_payload_changed,
)
from .review_payload import build_prompt_review_payload
from .review_publish import (
    PromptReviewPublishError,
    PromptReviewPublishErrorCode,
    publish_prompt_review,
)
from .review_submission import (
    PromptReviewSubmissionError,
    PromptReviewSubmissionErrorCode,
    submit_prompt_for_review,
)

#: Beta 11.11C4H/C4J: request-local (never module-global, cache, thread-local,
#: ContextVar or session) storage for the Beta 11.11C4G baseline captured by
#: ``PromptAdmin.save_model()`` (or, since Beta 11.11C4J, pre-populated by
#: ``PromptAdmin.revision_view()``/``recover_view()`` before the reversion
#: mutation runs), consumed by ``PromptAdmin.save_related()`` further down the
#: same request. Keyed by prompt pk so a hypothetical future multi-object save
#: on the same request cannot cross-contaminate. Lives only as long as the
#: request object itself. The attribute name predates C4J and still says
#: "baselines" - the values it holds are now :class:`_PromptReversionGuardState`,
#: not bare :class:`~prompts.review_edit_guard.PromptReviewEditBaseline`
#: instances; renaming the attribute was not worth the churn.
_PROMPT_REVIEW_EDIT_BASELINE_REQUEST_ATTR = "_mentoro_prompt_review_edit_baselines"


class _PromptReviewEditIntegrationError(RuntimeError):
    """
    An internal admin-lifecycle contract violation, never a routine outcome:
    ``save_related()`` found no state matching the prompt it is about to save
    relations for, or ``save_model()`` found an unexpected
    :data:`_PromptReversionGuardSource.NORMAL_CHANGEFORM` state already
    present. Under normal Django admin operation this can only mean
    ``save_model()`` did not run, did not succeed, or ran for a different
    prompt than ``save_related()`` is now handling - never "no payload
    change", which is a value ``invalidate_prompt_review_if_payload_changed()``
    itself reports. Deliberately private: no other module is meant to catch
    this specifically.
    """


class _PromptReversionGuardSource(StrEnum):
    """
    Beta 11.11C4J: which of the three lifecycles populated a Prompt's
    request-local guard state. ``save_model()``/``save_related()`` branch on
    this to decide whether to capture a fresh baseline themselves (normal
    changeform) or consume a state prepared earlier in the same request by
    ``revision_view()``/``recover_view()`` (revert/recover) - never both.
    """

    NORMAL_CHANGEFORM = "normal_changeform"
    REVISION_REVERT = "revision_revert"
    RECOVERY = "recovery"


@dataclass(frozen=True, slots=True)
class _PromptReversionGuardState:
    """
    Beta 11.11C4J: immutable, minimal request-local record of which guard
    lifecycle is in progress for one prompt id.

    Carries no payload, no model instance, no user instance, no revision
    instance and no connection object - only the identifiers
    ``save_model()``/``save_related()`` need to finish the lifecycle
    correctly. ``baseline`` is the Beta 11.11C4G payload baseline captured
    *before* the reversion mutation for :data:`_PromptReversionGuardSource.REVISION_REVERT`
    (and, unchanged from Beta 11.11C4H, before the ordinary root save for
    :data:`_PromptReversionGuardSource.NORMAL_CHANGEFORM`); it is always
    ``None`` for :data:`_PromptReversionGuardSource.RECOVERY`, since no prior
    row exists to baseline against.

    Beta 11.11D3C adds :attr:`target_revision_id` - the id of the revision the
    user actually selected, taken from the ``Version``
    ``revision_view()`` already resolved deterministically through
    ``_get_prompt_version_or_404()`` (content type *and* object id verified).
    It is a plain integer, never a ``Revision`` instance, and it is set only
    for :data:`_PromptReversionGuardSource.REVISION_REVERT`; a normal
    changeform and a recovery both leave it ``None``, because neither has a
    selected revision to restore a binding from.
    """

    source: _PromptReversionGuardSource
    prompt_id: int
    database_alias: str
    baseline: PromptReviewEditBaseline | None
    target_revision_id: int | None = None


@dataclass(frozen=True, slots=True)
class PromptRevisionGuardResult:
    """
    Beta 11.11C4J: immutable outcome of finishing a guarded
    ``save_related()`` call, uniform across all three
    :class:`_PromptReversionGuardSource` values - test convenience only,
    exactly like :class:`~prompts.review_edit_guard.PromptReviewEditGuardResult`
    (Django's own admin machinery never reads a ``save_related()`` return
    value).

    ``payload_changed`` is ``None`` for
    :data:`_PromptReversionGuardSource.RECOVERY` (no prior row to baseline
    against) and, since Beta 11.11D3C, also for
    :data:`_PromptReversionGuardSource.REVISION_REVERT` (the pre-revert
    baseline is deliberately not consulted - see
    ``_finish_prompt_review_guard()``). ``invalidation`` carries the real
    ``core.review_binding.ReviewInvalidationResult`` verbatim whenever B2B2
    actually ran - via Beta 11.11C4G's compare on a genuine changeform edit,
    via the Beta 11.11C4J restored-binding check after a revert, or via the
    Beta 11.11C4J unconditional recovery invalidation - and is ``None``
    whenever B2B2 never ran at all.
    """

    prompt_id: int
    database_alias: str
    source: _PromptReversionGuardSource
    payload_changed: bool | None
    invalidated: bool
    invalidation: ReviewInvalidationResult | None


def _prompt_review_edit_baselines(request) -> dict[int, _PromptReversionGuardState]:
    store = getattr(request, _PROMPT_REVIEW_EDIT_BASELINE_REQUEST_ATTR, None)
    if store is None:
        store = {}
        setattr(request, _PROMPT_REVIEW_EDIT_BASELINE_REQUEST_ATTR, store)
    return store


def _selected_action_name(request):
    """
    Determine which posted ``action`` value the user actually pressed
    (Beta 11.11C2B1A), closely enough to ``ModelAdmin.response_action`` to
    agree with it on every case that matters - without ever using private
    Django APIs or running anything itself.

    A changelist POST can carry more than one ``action`` field (the action
    bar is rendered above *and* below the list); ``index`` says which one was
    actually submitted. ``response_action`` resolves it as
    ``int(request.POST.get("index", 0))``, falling back to ``0`` whenever
    that conversion raises ``ValueError`` - so a missing, empty or
    non-numeric ``index`` all resolve to the *first* action field, exactly
    like a page that only renders one action bar. This function reproduces
    that default deliberately, because it is also what a test POST that
    omits ``index`` altogether means.

    A negative or out-of-range index is different: Python's own list
    indexing (or Django's ``except IndexError: pass`` fallback, which leaves
    the last posted value in place) would still resolve *some* value there,
    but never in a way this function is willing to trust blindly. So both
    fail closed here - returning ``None`` - even in the case where Django's
    own resolution would in fact land on ``action_submit_for_review``. A
    crafted index must never grant the VersionAdmin bypass by itself; if
    Django's real dispatch still selects the submit action from such a
    request, it does so inside the retained VersionAdmin revision context,
    where the submission primitive's own fail-closed check rejects it.
    """
    if request.method != "POST":
        return None

    action_values = request.POST.getlist("action")
    if not action_values:
        return None

    raw_index = request.POST.get("index")
    if raw_index is None:
        action_index = 0
    else:
        try:
            action_index = int(raw_index)
        except (TypeError, ValueError):
            action_index = 0

    if action_index < 0 or action_index >= len(action_values):
        return None

    selected_action = action_values[action_index]
    return selected_action or None


#: The Prompt admin actions that must run outside VersionAdmin's shared
#: revision context because each hands its selection to a per-root C3-series
#: primitive (Beta 11.11C2A / C3A / D2) that opens its own atomic transaction
#: and reversion revision per object. Extended in Beta 11.11C3B to cover
#: approval alongside submission, and in Beta 11.11D2 to cover publishing -
#: see ``PromptAdmin.changelist_view``.
_ISOLATED_PROMPT_ACTIONS = frozenset(
    {
        "action_submit_for_review",
        "action_approve",
        "action_publish",
    }
)


@admin.register(Prompt)
class PromptAdmin(EditorialWorkflowAdminMixin, TranslatableTinyMCEMixin, VersionAdmin):
    tinymce_fields = ("intro", "body", "outro")
    list_display = (
        "display_title", "pk", "status", "is_published", "author", "reviewed_by",
        "published_fmt", "updated_fmt",
    )
    list_filter = ("status", "author", "reviewed_by")
    search_fields = ("translations__title", "translations__intro", "translations__body", "translations__slug")
    ordering = ("-published_at", "-updated_at")
    date_hierarchy = "published_at"

    readonly_fields = (
        "status",
        "submitted_for_review_at",
        "reviewed_at",
        "reviewed_by",
        "live_i18n",
        "is_published",
        "public_slug",
        "updated_at",
        "last_published_revision_id",
    )

    fieldsets = (
        (_("Editorial"), {
            "fields": (
                "status",
                "author",
                "reviewed_by",
                "reviewed_at",
                "review_note",
            )
        }),
        (_("Meta"), {
            "fields": (
                "is_published",
                "published_at",
                "updated_at",
                "submitted_for_review_at",
                "last_published_revision_id",
                "tools",
            ),
        }),
        (_("Routing"), {
            "fields": ("slug", "public_slug"),
        }),
        (_("Content (translated)"), {
            "fields": ("title", "intro", "body", "outro"),
            "description": _("These fields are language-specific. Use the language tabs above."),
        }),
        (_("Internals"), {
            "classes": ("collapse",),
            "fields": ("live_i18n",),
        }),
    )

    #: C2A error codes the admin recovers from per prompt: it counts them,
    #: warns in aggregate, and keeps processing the rest of the selection.
    _RECOVERABLE_SUBMISSION_CODES = frozenset(
        {
            PromptReviewSubmissionErrorCode.STATUS_NOT_SUBMITTABLE,
            PromptReviewSubmissionErrorCode.OBJECT_NOT_FOUND,
        }
    )
    #: Request/configuration problems (bad alias or actor): fail closed - stop
    #: processing further prompts and show one generic error - but leave every
    #: prompt already committed by C2A's own per-root transaction untouched.
    _CONFIG_SUBMISSION_CODES = frozenset(
        {
            PromptReviewSubmissionErrorCode.INVALID_DATABASE_ALIAS,
            PromptReviewSubmissionErrorCode.DATABASE_ALIAS_MISMATCH,
            PromptReviewSubmissionErrorCode.INVALID_ACTOR,
            PromptReviewSubmissionErrorCode.ACTOR_DATABASE_ALIAS_MISMATCH,
        }
    )

    def changelist_view(self, request, extra_context=None):
        """
        Run the prompt submit-for-review, approve and publish actions
        *outside* VersionAdmin's shared revision context (Beta 11.11C2B,
        hardened in Beta 11.11C2B1A, extended to approval in Beta 11.11C3B
        and to publishing in Beta 11.11D2).

        ``reversion.admin.VersionAdmin.changelist_view`` wraps the entire
        changelist POST - including admin-action dispatch - in one
        ``reversion.create_revision()`` block. That is exactly what the shared
        editorial submit/approve/publish actions relied on to batch every
        selected object into a single revision. This admin's submit, approve
        and publish actions instead delegate each prompt to
        ``submit_prompt_for_review`` (Beta 11.11C2A), ``approve_prompt_review``
        (Beta 11.11C3A) or ``publish_prompt_review`` (Beta 11.11D2), each of
        which opens its own per-root revision and, by design, refuses to run
        inside an outer reversion context. So for any of those three actions
        (:data:`_ISOLATED_PROMPT_ACTIONS`) we dispatch through
        ``ModelAdmin.changelist_view`` (``super(VersionAdmin, self)``),
        skipping only VersionAdmin's revision wrapper; every other request -
        the GET changelist, and the rework/archive/restore actions that still
        write directly inside a revision - keeps VersionAdmin's normal
        behaviour.

        Which action was actually selected is decided by
        ``_selected_action_name``, not by a bare ``request.POST.get("action")``
        - a changelist POST can carry more than one ``action`` field (top and
        bottom action bars), and only the one ``index`` actually points at
        counts. See that function's docstring for the exact contract,
        including its fail-closed behaviour for a negative or out-of-range
        index. That contract is reused verbatim here - only the set of action
        names it is compared against grew from one to three.
        """
        if request.method == "POST" and _selected_action_name(request) in _ISOLATED_PROMPT_ACTIONS:
            return super(VersionAdmin, self).changelist_view(request, extra_context)
        return super().changelist_view(request, extra_context)

    @admin.action(description=_("Send to review"))
    def action_submit_for_review(self, request, queryset):
        """
        Prompt-specific override of the shared editorial submit action
        (Beta 11.11C2B).

        The shared ``EditorialWorkflowAdminMixin.action_submit_for_review``
        wraps the *whole* selection in one ``transaction.atomic()`` and one
        ``reversion.create_revision()``, so every selected prompt lands in a
        single shared revision - exactly the "which revision was this review
        bound to?" ambiguity Beta 11.11A flagged. This override instead hands
        each prompt to ``prompts.review_submission.submit_prompt_for_review``
        (Beta 11.11C2A), which gives every prompt its own atomic transaction,
        its own reversion revision, its own captured-revision binding and its
        own canonical fingerprint.

        Consequently the admin itself opens no ``transaction.atomic()`` and no
        ``reversion.create_revision()``, calls no FSM transition and no model
        save, and never touches ``set_user``/``set_comment`` - all of that is
        C2A's responsibility. A failure on one prompt therefore cannot roll back
        another's already-committed submission.

        The selection is materialised in a stable primary-key order *before*
        any submission runs, because a successful submit changes a prompt's
        status and would otherwise shift a still-evaluating queryset. The
        per-object editorial permission check
        (``content.submit_for_review``) is preserved exactly, and denied
        objects are skipped without ever reaching C2A - so no revision or
        version is written for them.

        C2A error codes are classified, never blanket-caught:

        * recoverable (:data:`_RECOVERABLE_SUBMISSION_CODES`) - counted, warned
          in aggregate, processing continues;
        * request/configuration (:data:`_CONFIG_SUBMISSION_CODES`) - fail
          closed: a single generic error message (no internal codes, aliases or
          revision ids) and processing stops, while earlier successes stay
          committed;
        * anything else (integrity/programmer errors such as
          ``ROOT_VERSION_MISSING`` or ``PAYLOAD_CHANGED_DURING_SUBMISSION``) is
          re-raised, never disguised as a harmless skip.
        """
        # Materialise the selection once, in a stable order, so a submitted
        # prompt leaving the queryset's implicit filter cannot skew iteration.
        selected_prompts = list(queryset.order_by("pk"))
        db_alias = queryset.db

        submitted = 0
        not_submittable = 0
        missing = 0
        permission_denied = 0
        config_error = False

        for prompt in selected_prompts:
            if not self._user_has_perm(request, "submit_for_review", prompt):
                permission_denied += 1
                continue
            try:
                submit_prompt_for_review(prompt, actor=request.user, using=db_alias)
            except PromptReviewSubmissionError as exc:
                if exc.code == PromptReviewSubmissionErrorCode.STATUS_NOT_SUBMITTABLE:
                    not_submittable += 1
                    continue
                if exc.code == PromptReviewSubmissionErrorCode.OBJECT_NOT_FOUND:
                    missing += 1
                    continue
                if exc.code in self._CONFIG_SUBMISSION_CODES:
                    config_error = True
                    break
                # Integrity/programmer error - not a routine skip. Re-raise so
                # it surfaces; prompts already committed by C2A remain committed.
                raise
            submitted += 1

        if submitted:
            self.message_user(
                request,
                ngettext(
                    "%(count)d prompt was submitted for review.",
                    "%(count)d prompts were submitted for review.",
                    submitted,
                )
                % {"count": submitted},
                level=messages.SUCCESS,
            )
        if not_submittable:
            self.message_user(
                request,
                ngettext(
                    "%(count)d prompt was skipped because its status does not allow "
                    "submission for review.",
                    "%(count)d prompts were skipped because their status does not allow "
                    "submission for review.",
                    not_submittable,
                )
                % {"count": not_submittable},
                level=messages.WARNING,
            )
        if missing:
            self.message_user(
                request,
                ngettext(
                    "%(count)d selected prompt no longer exists and was skipped.",
                    "%(count)d selected prompts no longer exist and were skipped.",
                    missing,
                )
                % {"count": missing},
                level=messages.WARNING,
            )
        if permission_denied:
            self.message_user(
                request,
                ngettext(
                    "%(count)d prompt was skipped because you are not allowed to "
                    "submit it.",
                    "%(count)d prompts were skipped because you are not allowed to "
                    "submit them.",
                    permission_denied,
                )
                % {"count": permission_denied},
                level=messages.WARNING,
            )
        if config_error:
            self.message_user(
                request,
                _(
                    "Some prompts could not be submitted because of a server "
                    "configuration problem. Please contact an administrator."
                ),
                level=messages.ERROR,
            )

    #: C3A error codes the admin recovers from per prompt: it counts them,
    #: warns in aggregate, and keeps processing the rest of the selection.
    #: ``REVIEW_PAYLOAD_CHANGED`` belongs here deliberately - until the C4 edit
    #: invalidation exists, a prompt whose reviewed content changed after
    #: submit is an expected editorial state, not a technical failure.
    _RECOVERABLE_APPROVAL_CODES = frozenset(
        {
            PromptReviewApprovalErrorCode.STATUS_NOT_APPROVABLE,
            PromptReviewApprovalErrorCode.REVIEW_PAYLOAD_CHANGED,
            PromptReviewApprovalErrorCode.OBJECT_NOT_FOUND,
        }
    )
    #: Request/configuration problems (bad alias or actor): fail closed - stop
    #: processing further prompts and show one generic error - but leave every
    #: prompt already committed by C3A's own per-root transaction untouched.
    _CONFIG_APPROVAL_CODES = frozenset(
        {
            PromptReviewApprovalErrorCode.INVALID_DATABASE_ALIAS,
            PromptReviewApprovalErrorCode.DATABASE_ALIAS_MISMATCH,
            PromptReviewApprovalErrorCode.INVALID_ACTOR,
            PromptReviewApprovalErrorCode.ACTOR_DATABASE_ALIAS_MISMATCH,
        }
    )

    @admin.action(description=_("Approve (Review → Approved)"))
    def action_approve(self, request, queryset):
        """
        Prompt-specific override of the shared editorial approve action
        (Beta 11.11C3B).

        The shared ``EditorialWorkflowAdminMixin.action_approve`` wraps the
        *whole* selection in one ``transaction.atomic()`` and one
        ``reversion.create_revision()``, so every selected prompt's approval
        lands in a single shared revision. This override instead hands each
        prompt to ``prompts.review_approval.approve_prompt_review`` (Beta
        11.11C3A), which gives every prompt its own atomic transaction, its
        own reversion revision, and binds ``approved_revision`` to the exact
        ``review_revision`` already captured at submit time - never a fresh
        lookup, never a new snapshot.

        Consequently the admin itself opens no ``transaction.atomic()`` and no
        ``reversion.create_revision()``, calls no FSM transition and no model
        save, computes no payload or fingerprint, validates no binding, and
        never touches ``set_user``/``set_comment`` - all of that is C3A's
        responsibility. A failure on one prompt therefore cannot roll back
        another's already-committed approval.

        The selection is materialised in a stable primary-key order *before*
        any approval runs, for the same reason as the submit override: a
        successful approval changes a prompt's status and would otherwise
        shift a still-evaluating queryset. The per-object editorial permission
        check (``content.approve``) is preserved exactly, and denied objects
        are skipped without ever reaching C3A - so no revision or version is
        written for them.

        C3A error codes are classified, never blanket-caught:

        * recoverable (:data:`_RECOVERABLE_APPROVAL_CODES`) - counted, warned
          in aggregate, processing continues. This includes
          ``REVIEW_PAYLOAD_CHANGED``: until the C4 edit-invalidation slice, a
          prompt edited after submit is an expected stale-review state, not an
          integrity failure, and the admin never claims to have moved it back
          to draft or rework - it simply was not approved;
        * request/configuration (:data:`_CONFIG_APPROVAL_CODES`) - fail
          closed: a single generic error message (no internal codes, aliases
          or revision ids) and processing stops, while earlier successes stay
          committed;
        * anything else (integrity/programmer errors such as
          ``REVIEW_BINDING_INVALID`` or ``PAYLOAD_CHANGED_DURING_APPROVAL``) is
          re-raised, never disguised as a harmless skip.
        """
        # Materialise the selection once, in a stable order, so an approved
        # prompt leaving the queryset's implicit filter cannot skew iteration.
        selected_prompts = list(queryset.order_by("pk"))
        db_alias = queryset.db

        approved = 0
        not_approvable = 0
        stale = 0
        missing = 0
        permission_denied = 0
        config_error = False

        for prompt in selected_prompts:
            if not self._user_has_perm(request, "approve", prompt):
                permission_denied += 1
                continue
            try:
                approve_prompt_review(prompt, actor=request.user, using=db_alias)
            except PromptReviewApprovalError as exc:
                if exc.code == PromptReviewApprovalErrorCode.STATUS_NOT_APPROVABLE:
                    not_approvable += 1
                    continue
                if exc.code == PromptReviewApprovalErrorCode.REVIEW_PAYLOAD_CHANGED:
                    stale += 1
                    continue
                if exc.code == PromptReviewApprovalErrorCode.OBJECT_NOT_FOUND:
                    missing += 1
                    continue
                if exc.code in self._CONFIG_APPROVAL_CODES:
                    config_error = True
                    break
                # Integrity/programmer error - not a routine skip. Re-raise so
                # it surfaces; prompts already committed by C3A remain committed.
                raise
            approved += 1

        if approved:
            self.message_user(
                request,
                ngettext(
                    "%(count)d prompt was approved.",
                    "%(count)d prompts were approved.",
                    approved,
                )
                % {"count": approved},
                level=messages.SUCCESS,
            )
        if not_approvable:
            self.message_user(
                request,
                ngettext(
                    "%(count)d prompt was skipped because its status does not allow "
                    "approval.",
                    "%(count)d prompts were skipped because their status does not allow "
                    "approval.",
                    not_approvable,
                )
                % {"count": not_approvable},
                level=messages.WARNING,
            )
        if stale:
            self.message_user(
                request,
                ngettext(
                    "%(count)d prompt was skipped because its reviewed content has "
                    "changed.",
                    "%(count)d prompts were skipped because their reviewed content has "
                    "changed.",
                    stale,
                )
                % {"count": stale},
                level=messages.WARNING,
            )
        if missing:
            self.message_user(
                request,
                ngettext(
                    "%(count)d selected prompt no longer exists and was skipped.",
                    "%(count)d selected prompts no longer exist and were skipped.",
                    missing,
                )
                % {"count": missing},
                level=messages.WARNING,
            )
        if permission_denied:
            self.message_user(
                request,
                ngettext(
                    "%(count)d prompt was skipped because you are not allowed to "
                    "approve it.",
                    "%(count)d prompts were skipped because you are not allowed to "
                    "approve them.",
                    permission_denied,
                )
                % {"count": permission_denied},
                level=messages.WARNING,
            )
        if config_error:
            self.message_user(
                request,
                _(
                    "Some prompts could not be approved because of a server "
                    "configuration problem. Please contact an administrator."
                ),
                level=messages.ERROR,
            )

    #: D2 error codes the admin recovers from per prompt: it counts them,
    #: warns in aggregate, and keeps processing the rest of the selection.
    #: These are all legitimate editorial states of a *selected but not
    #: publishable* prompt, never technical failures - a prompt that is not
    #: approved, whose approved content has since changed (C4G/C4H/D1 will
    #: have moved it back to draft), that has no complete translation, or
    #: that someone else deleted meanwhile.
    _RECOVERABLE_PUBLISH_CODES = frozenset(
        {
            PromptReviewPublishErrorCode.STATUS_NOT_PUBLISHABLE,
            PromptReviewPublishErrorCode.REVIEW_PAYLOAD_CHANGED,
            PromptReviewPublishErrorCode.NO_PUBLISHABLE_TRANSLATION,
            PromptReviewPublishErrorCode.INCOMPLETE_TRANSLATION,
            PromptReviewPublishErrorCode.OBJECT_NOT_FOUND,
            PromptReviewPublishErrorCode.PERMISSION_DENIED,
        }
    )
    #: Request/configuration problems (bad alias or actor): fail closed - stop
    #: processing further prompts and show one generic error - but leave every
    #: prompt already committed by D2's own per-root transaction untouched.
    _CONFIG_PUBLISH_CODES = frozenset(
        {
            PromptReviewPublishErrorCode.INVALID_DATABASE_ALIAS,
            PromptReviewPublishErrorCode.DATABASE_ALIAS_MISMATCH,
            PromptReviewPublishErrorCode.INVALID_ACTOR,
            PromptReviewPublishErrorCode.ACTOR_DATABASE_ALIAS_MISMATCH,
        }
    )

    @admin.action(description=_("Publish (Approved → Published)"))
    def action_publish(self, request, queryset):
        """
        Prompt-specific override of the shared editorial publish action
        (Beta 11.11D2).

        The shared ``EditorialWorkflowAdminMixin.action_publish`` wraps the
        *whole* selection in one ``transaction.atomic()`` and one
        ``reversion.create_revision()``, calls the FSM transition with a
        ``note`` that overwrites ``review_note``, saves, and then asks
        ``set_last_published_revision()`` for the marker - a helper that
        resolves it with an unordered ``Version.objects...first()`` from
        *inside* the still-open revision block, before reversion has written
        that revision's versions at all, so the marker could only ever point
        at an older version. It also validated no binding and no fingerprint,
        so a prompt whose approved content had since changed was published
        anyway.

        This override instead hands each prompt to
        ``prompts.review_publish.publish_prompt_review`` (Beta 11.11D2), which
        gives every prompt its own atomic transaction and its own reversion
        revision, re-validates the review and approval bindings and the
        approved fingerprint against what is on disk immediately before the
        transition, applies the public-slug policy before the live snapshot is
        built, and resolves the marker by an exact
        ``(revision, content_type, object_id, db)`` lookup after the revision
        is closed. A publish that cannot satisfy all of that is rolled back
        whole rather than half-applied.

        Consequently the admin itself opens no ``transaction.atomic()`` and no
        ``reversion.create_revision()``, calls no FSM transition and no model
        save, computes no payload or fingerprint, writes no marker, and never
        touches ``set_user``/``set_comment`` - all of that is D2's
        responsibility. A failure on one prompt therefore cannot roll back
        another's already-committed publish.

        The selection is materialised in a stable primary-key order *before*
        any publish runs, for the same reason as the submit and approve
        overrides: a successful publish changes a prompt's status and would
        otherwise shift a still-evaluating queryset. The per-object editorial
        permission check (``content.publish``) is preserved exactly, and
        denied objects are skipped without ever reaching D2 - so no revision
        or version is written for them.

        D2 error codes are classified, never blanket-caught: recoverable
        (:data:`_RECOVERABLE_PUBLISH_CODES`) are counted and warned in
        aggregate; request/configuration codes
        (:data:`_CONFIG_PUBLISH_CODES`) fail closed with one generic message
        and stop processing; anything else - an invalid binding, a missing or
        ambiguous root version, a failed postcondition - is re-raised, never
        disguised as a harmless skip.
        """
        # Materialise the selection once, in a stable order, so a published
        # prompt leaving the queryset's implicit filter cannot skew iteration.
        selected_prompts = list(queryset.order_by("pk"))
        db_alias = queryset.db

        published = 0
        not_publishable = 0
        stale = 0
        incomplete = 0
        missing = 0
        permission_denied = 0
        config_error = False

        for prompt in selected_prompts:
            if not self._user_has_perm(request, "publish", prompt):
                permission_denied += 1
                continue
            try:
                publish_prompt_review(prompt, actor=request.user, using=db_alias)
            except PromptReviewPublishError as exc:
                if exc.code == PromptReviewPublishErrorCode.STATUS_NOT_PUBLISHABLE:
                    not_publishable += 1
                    continue
                if exc.code == PromptReviewPublishErrorCode.REVIEW_PAYLOAD_CHANGED:
                    stale += 1
                    continue
                if exc.code in (
                    PromptReviewPublishErrorCode.NO_PUBLISHABLE_TRANSLATION,
                    PromptReviewPublishErrorCode.INCOMPLETE_TRANSLATION,
                ):
                    incomplete += 1
                    continue
                if exc.code == PromptReviewPublishErrorCode.OBJECT_NOT_FOUND:
                    missing += 1
                    continue
                if exc.code == PromptReviewPublishErrorCode.PERMISSION_DENIED:
                    # The admin already checked, so this means the locked row
                    # disagrees with the copy the changelist loaded - e.g. the
                    # author changed concurrently. A skip, not a server error.
                    permission_denied += 1
                    continue
                if exc.code in self._CONFIG_PUBLISH_CODES:
                    config_error = True
                    break
                # Integrity/programmer error - not a routine skip. Re-raise so
                # it surfaces; prompts already committed by D2 remain committed.
                raise
            published += 1

        if published:
            self.message_user(
                request,
                ngettext(
                    "%(count)d prompt was published.",
                    "%(count)d prompts were published.",
                    published,
                )
                % {"count": published},
                level=messages.SUCCESS,
            )
        if not_publishable:
            self.message_user(
                request,
                ngettext(
                    "%(count)d prompt was skipped because its status does not allow "
                    "publishing.",
                    "%(count)d prompts were skipped because their status does not allow "
                    "publishing.",
                    not_publishable,
                )
                % {"count": not_publishable},
                level=messages.WARNING,
            )
        if stale:
            self.message_user(
                request,
                ngettext(
                    "%(count)d prompt was skipped because its approved content has "
                    "changed and needs a new review.",
                    "%(count)d prompts were skipped because their approved content has "
                    "changed and needs a new review.",
                    stale,
                )
                % {"count": stale},
                level=messages.WARNING,
            )
        if incomplete:
            self.message_user(
                request,
                ngettext(
                    "%(count)d prompt was skipped because it has no complete "
                    "translation to publish.",
                    "%(count)d prompts were skipped because they have no complete "
                    "translation to publish.",
                    incomplete,
                )
                % {"count": incomplete},
                level=messages.WARNING,
            )
        if missing:
            self.message_user(
                request,
                ngettext(
                    "%(count)d selected prompt no longer exists and was skipped.",
                    "%(count)d selected prompts no longer exist and were skipped.",
                    missing,
                )
                % {"count": missing},
                level=messages.WARNING,
            )
        if permission_denied:
            self.message_user(
                request,
                ngettext(
                    "%(count)d prompt was skipped because you are not allowed to "
                    "publish it.",
                    "%(count)d prompts were skipped because you are not allowed to "
                    "publish them.",
                    permission_denied,
                )
                % {"count": permission_denied},
                level=messages.WARNING,
            )
        if config_error:
            self.message_user(
                request,
                _(
                    "Some prompts could not be published because of a server "
                    "configuration problem. Please contact an administrator."
                ),
                level=messages.ERROR,
            )

    def save_model(self, request, obj, form, change):
        """
        Beta 11.11C4H: capture the pre-mutation Beta 11.11C1/C4D v2 payload
        baseline for an *existing* Prompt, strictly before the root is saved.

        Only for ``change=True`` with an already-assigned pk - a brand-new
        Prompt has no prior review/approval state to protect, so Add takes
        the plain, unmodified path. The baseline is captured read-only,
        against the database (never against ``obj``, which the validated
        form has already mutated in memory - Beta 11.11C4G ignores that and
        rereads the row fresh), strictly before ``super().save_model()`` ever
        runs. If capture itself fails, ``super().save_model()`` is never
        called and nothing is mutated.

        The baseline is only stored on the request - for ``save_related()``
        to consume - once ``super().save_model()`` has actually succeeded. If
        it raises, execution never reaches that line, so no stale baseline is
        ever left behind; the surrounding admin changeform's own
        ``transaction.atomic()`` (``ModelAdmin.changeform_view()``) is what
        rolls the partial root save back, not this method.

        Beta 11.11C4J: when this same request is a reversion "revert"/
        "recover" confirmation, ``revision_view()``/``recover_view()`` already
        placed a :class:`_PromptReversionGuardState` for this exact prompt id
        on the request *before* the reversion mutation ran - i.e. before this
        method was ever reached. In that case the C4H capture above would
        read the database *after* ``reversion.revision.revert()`` already
        wrote the historical content, which is too late to serve as a
        "before" baseline (see the module-level design note above
        ``revision_view()``). This method must therefore never capture a new
        baseline nor overwrite that state - it only runs the ordinary
        ``super().save_model()`` root save and leaves the pre-populated state
        for ``save_related()`` to consume.

        Beta 11.11C4J (Abnahme): in every branch that actually persists an
        existing prompt, ``_preserve_untouched_translated_fields()`` runs
        strictly before ``super().save_model()`` so a readonly translated
        field the changeform never carries (``public_slug``) keeps its real,
        already-stored database value - the live value on a normal edit, the
        just-reverted historical value inside a revert/recover lifecycle - see
        that helper for the full rationale.
        """
        if not (change and obj.pk):
            super().save_model(request, obj, form, change)
            return

        store = _prompt_review_edit_baselines(request)
        existing = store.get(obj.pk)

        if existing is not None:
            if existing.source == _PromptReversionGuardSource.NORMAL_CHANGEFORM:
                raise _PromptReviewEditIntegrationError(
                    f"a review-edit baseline for prompt #{obj.pk} already exists "
                    "on this request"
                )
            # Beta 11.11C4J: revision_revert / recovery - the state was
            # deliberately prepared before the reversion mutation ran; do not
            # capture again and do not overwrite it.
            self._preserve_untouched_translated_fields(request, obj, form)
            super().save_model(request, obj, form, change)
            return

        baseline = capture_prompt_review_edit_baseline(obj)
        self._preserve_untouched_translated_fields(request, obj, form)
        super().save_model(request, obj, form, change)
        store[obj.pk] = _PromptReversionGuardState(
            source=_PromptReversionGuardSource.NORMAL_CHANGEFORM,
            prompt_id=baseline.prompt_id,
            database_alias=baseline.database_alias,
            baseline=baseline,
        )

    def _preserve_untouched_translated_fields(self, request, obj, form):
        """
        Beta 11.11C4J (Abnahme): restore translated fields that this admin's
        changeform does not carry as editable fields - today only
        ``public_slug`` (it is ``readonly`` and never rendered) - from the
        current, already-persisted database row for the form's active
        language, onto the exact translation instance the changeform is about
        to save.

        Why this is needed. Parler caches each translation in Django's cache
        framework. A value that reached the database *without* going through
        Parler's own ``save()`` - most importantly ``reversion.Revision.revert()``
        writing the historical translation rows directly, but also any raw
        ``QuerySet.update()`` - leaves that cache holding a *stale* translation.
        ``TranslatableAdmin``'s form then loads the stale translation, and
        because ``public_slug`` is absent from the form it is never
        re-populated; the subsequent ``obj.save()`` writes the stale value
        (typically ``None``) back over the real one whenever any *other*
        translated field on the same row changed (Parler only re-saves a
        translation that is dirty). Reading the field fresh here - a plain
        ``.values()`` query that bypasses the Parler cache entirely - and
        assigning it onto the current-language translation restores the correct
        value in every case: the live production value for a normal edit, and
        the just-reverted historical value inside a C4J revert/recover
        lifecycle (``revision.revert()`` has already run, in the same
        transaction, by the time this method is reached).

        Strictly scoped and side-effect free: uses the same database alias as
        the object (never a hardcoded ``"default"``) and the form's active
        language only (never a Parler fallback, never another language); never
        creates a translation that does not exist (a brand-new language tab
        keeps ``public_slug=None`` because the fresh read returns nothing);
        never derives ``public_slug`` from ``slug``; never touches
        ``form.initial``/``form.cleaned_data``/``form.changed_data``; and
        preserves exactly the translated fields absent from *this* form, not
        an assumed hardcoded list.
        """
        preserved = [
            name
            for name in PromptTranslation.get_translated_fields()
            if name not in form.fields
        ]
        if not preserved:
            return

        language_code = self.get_form_language(request, obj)
        db_alias = obj._state.db or DEFAULT_DB_ALIAS
        row = (
            PromptTranslation.objects.using(db_alias)
            .filter(master_id=obj.pk, language_code=language_code)
            .values(*preserved)
            .first()
        )
        if row is None:
            return

        obj.set_current_language(language_code)
        for field_name in preserved:
            setattr(obj, field_name, row[field_name])

    def _auto_transition_to_review(self, request, obj):
        """
        Beta 11.11C4J (Abnahme): inside a request-local C4J revert/recover
        lifecycle, the general "a published object was edited through the
        changeform, move it to review" auto-transition on
        ``EditorialWorkflowAdminMixin`` must not fire.

        A ``reversion.Revision.revert()`` restores the historical row before
        Django's ``changeform_view()`` builds its confirm form; Parler's
        translated-field form initials can lag that same-transaction write, so
        ``form.has_changed()`` reports a spurious change even when the confirm
        POST submits exactly the reverted content. When the reverted-*from*
        row was ``published``, that spurious "change" alone would otherwise
        drive the general auto-review (published -> review), which C4J's own
        compare would then invalidate to draft/rework - the wrong outcome:
        reverting to a published version must end published. The historical
        version sets the restored status; from there only C4J / Beta 11.11B2B2
        decide (review/approved with a real payload change or an invalid
        restored binding -> draft/rework; every other status left as restored).

        This override bypasses *only* that one general auto-transition, and
        *only* when a request-local :data:`_PromptReversionGuardSource.REVISION_REVERT`
        or :data:`_PromptReversionGuardSource.RECOVERY` state exists for this
        exact prompt. Everything else the mixin's ``save_model`` does - the
        real root/translation save, the author default, the
        ``last_published_revision_id`` handling, permissions, logging - runs
        unchanged, and every non-reversion save (normal changeform, add,
        changelist actions, preview) takes the full, unmodified mixin path,
        so the pre-existing published-edit contract is preserved exactly.
        """
        state = _prompt_review_edit_baselines(request).get(getattr(obj, "pk", None))
        if state is not None and state.source in (
            _PromptReversionGuardSource.REVISION_REVERT,
            _PromptReversionGuardSource.RECOVERY,
        ):
            return
        return super()._auto_transition_to_review(request, obj)

    def save_related(self, request, form, formsets, change):
        """
        Beta 11.11C4H: after ``super().save_related()`` has persisted
        everything else the canonical payload can depend on - tag/tool
        membership via ``form.save_m2m()``, and any formsets (Prompt has
        none) - compare the current v2 payload against the baseline
        ``save_model()`` captured, and invalidate a bound review/approval via
        the existing Beta 11.11B2B2 primitive exactly once if, and only if,
        it actually changed.

        For ``change=True`` the matching state is popped off the request
        immediately, *before* ``super().save_related()`` runs - never left
        for a second, accidental compare, and never reused across requests. A
        missing state at this point is an internal lifecycle contract
        violation (``save_model()`` did not run, or did not succeed, for this
        exact prompt) and fails closed *before* any relation is saved -
        never silently treated as "no payload change" and never a
        ``messages.warning`` that lets the save continue unprotected.

        Beta 11.11C4J: which primitive finishes the lifecycle depends on
        :attr:`_PromptReversionGuardState.source` - see
        ``_finish_prompt_review_guard()``. This still runs *inside* the same
        active ``reversion.create_revision()`` block VersionAdmin's
        ``revision_view()``/``recover_view()`` opened (Django's admin
        ``_changeform_view()`` never returns control to those callers before
        ``save_related()`` has already run), so any B2B2 save this triggers
        is recorded into that one, already-open revision - never a second one.

        Returns a :class:`~prompts.review_edit_guard.PromptReviewEditGuardResult`
        or :class:`PromptRevisionGuardResult` (``None`` for Add, or for a
        change whose relations were saved without ever reaching compare)
        purely for direct-call test convenience - Django's own admin
        machinery never reads a ``save_related()`` return value.
        """
        if change and form.instance.pk:
            prompt_id = form.instance.pk
            state = _prompt_review_edit_baselines(request).pop(prompt_id, None)
            if state is None:
                raise _PromptReviewEditIntegrationError(
                    f"no captured review-edit state for prompt #{prompt_id}; "
                    "save_model() must run and succeed before save_related()"
                )
            super().save_related(request, form, formsets, change)
            return self._finish_prompt_review_guard(state, form.instance)
        super().save_related(request, form, formsets, change)
        return None

    def _finish_prompt_review_guard(self, state, instance):
        """
        Beta 11.11C4J: the single point where every ``save_related()`` call -
        regardless of :class:`_PromptReversionGuardSource` - finishes the
        review-binding guard lifecycle. Dispatches to exactly one of three
        shapes, never inventing a fourth:

        * :data:`_PromptReversionGuardSource.NORMAL_CHANGEFORM` - unchanged
          Beta 11.11C4H behaviour: one Beta 11.11C4G compare, returned as-is.
        * :data:`_PromptReversionGuardSource.REVISION_REVERT` - only the
          *restored* binding is judged, never the pre-revert baseline.

          Beta 11.11C4J originally compared the restored payload against the
          Beta 11.11C4G baseline captured before ``revert()`` and treated any
          difference as an ordinary edit. For a rollback that comparison is
          almost always true and asks the wrong question: the user
          deliberately discarded the pre-revert state, so "differs from what
          I just threw away" says nothing about whether the restored row is
          self-consistent. Its practical effect was that rolling back to a
          genuinely ``approved`` revision landed in ``draft``, destroying the
          very state the rollback was meant to reproduce (Beta 11.11D3C).

          D3C therefore always runs
          ``_invalidate_reverted_prompt_if_binding_invalid()`` instead: the
          restored ``review``/``approved`` binding is checked for structural
          validity (``core.review_binding.validate_review_binding``/
          ``validate_approved_binding``) and for a stale fingerprint (the
          restored ``review_payload_fingerprint`` compared against the live
          canonical fingerprint, both already-computed values - never a
          re-derived hashing/serialization implementation). That check is
          *content-based*, so it doubles as the discriminator D3C needs
          without any POST or form heuristic: a pure rollback restores a
          fingerprint that still matches its own content and is preserved,
          while a rollback the user additionally edited in the same form no
          longer matches and is invalidated through the ordinary B2B2 path.
          A revision whose stored binding was never provable in the first
          place (Beta 11.11C2A never binds ``review_revision`` inside the
          revision it captures) stays fail-closed and invalidates, exactly as
          C4J intended.

          ``payload_changed`` is therefore ``None`` here, as it already is
          for recovery - no baseline comparison is performed at all. The
          baseline is still captured in ``revision_view()``: a capture
          failure must still abort the whole revert before it mutates
          anything.
        * :data:`_PromptReversionGuardSource.RECOVERY` - no baseline exists
          (the row did not exist before), so no payload compare is possible
          or meaningful. The final, fully-restored row is handed to B2B2
          exactly once, unconditionally - B2B2's own existing
          "review/approved only, everything else is a no-op" contract is
          reused verbatim, never re-implemented here.
        """
        if state.source == _PromptReversionGuardSource.RECOVERY:
            invalidation = invalidate_editorial_review_state(
                instance, using=state.database_alias
            )
            return PromptRevisionGuardResult(
                prompt_id=state.prompt_id,
                database_alias=state.database_alias,
                source=state.source,
                payload_changed=None,
                invalidated=invalidation.changed,
                invalidation=invalidation,
            )

        if state.source == _PromptReversionGuardSource.NORMAL_CHANGEFORM:
            return invalidate_prompt_review_if_payload_changed(
                instance, baseline=state.baseline, using=state.database_alias,
            )

        # _PromptReversionGuardSource.REVISION_REVERT
        #
        # Beta 11.11D3C: a restored ``review`` row first gets the chance to
        # have its binding provably reconstructed against the selected
        # revision. Only if that cannot be proven does the existing
        # fail-closed check run and take the row to ``draft``. Every other
        # restored status skips the reconstruction entirely (the helper
        # returns False for them) and is handled exactly as before.
        if self._restore_reverted_prompt_review_binding_if_provable(
            instance,
            revision_id=state.target_revision_id,
            using=state.database_alias,
        ):
            return PromptRevisionGuardResult(
                prompt_id=state.prompt_id,
                database_alias=state.database_alias,
                source=state.source,
                payload_changed=None,
                invalidated=False,
                invalidation=None,
            )

        stale_invalidation = self._invalidate_reverted_prompt_if_binding_invalid(
            instance, using=state.database_alias,
        )
        return PromptRevisionGuardResult(
            prompt_id=state.prompt_id,
            database_alias=state.database_alias,
            source=state.source,
            payload_changed=None,
            invalidated=stale_invalidation is not None and stale_invalidation.changed,
            invalidation=stale_invalidation,
        )

    def _live_review_payload_fingerprint(self, prompt, *, using):
        """
        Beta 11.11D3C1: the single sanctioned place in this whole module that
        touches the canonical payload builder directly.

        Beta 11.11C4J established the contract that ``prompts/admin.py`` reuses
        :func:`prompts.review_payload.build_prompt_review_payload` from exactly
        one call site - not because a second *purpose* would be illegitimate,
        but so that "which payload does the admin compare against?" has exactly
        one answer, reviewable in one place, with no chance of a second site
        drifting into a differently-built (locale-, alias- or refresh-dependent)
        payload while still calling itself canonical. Beta 11.11D3C then needed
        the very same value for a second, genuinely different question (is the
        restored fingerprint still provably the fingerprint of the restored
        content?) and added a second direct call, which broke that contract
        rather than reusing it.

        Both questions need one and the same value: the canonical fingerprint
        of the row as it currently stands on ``using``. So both go through here.

        Deliberately nothing but ``builder -> fingerprint``: no status change,
        no binding write, no invalidation, no caching, no refresh of its own, no
        request-local state, no swallowed exception. ``prompt`` must already be
        the freshly read row the caller wants judged (both callers re-read it on
        ``using`` themselves), and ``using`` is always passed on explicitly -
        never defaulted to ``"default"`` - so a revert running on a non-default
        alias is fingerprinted on that alias too. Every
        :class:`~prompts.review_payload.PromptReviewPayloadError` and every
        :class:`core.review_binding.ReviewPayloadFingerprintError` propagates
        unchanged and aborts the surrounding revert transaction.
        """
        return fingerprint_review_payload(
            build_prompt_review_payload(prompt, using=using)
        )

    def _restore_reverted_prompt_review_binding_if_provable(
        self, instance, *, revision_id, using
    ):
        """
        Beta 11.11D3C: rebind a reverted ``review`` prompt to the revision the
        user actually selected - but only on proof.

        Why this is needed at all. ``prompts.review_submission`` cannot store
        a submit revision's own id inside that revision: while the revision is
        being written the id does not exist yet, so the binding is set in a
        second, targeted save *after* the reversion context closes (that
        module's steps 22-23). A submit snapshot therefore serializes
        ``status="review"`` and the real canonical fingerprint, but
        ``review_revision = None``. Reverting to it restores a row whose
        binding looks missing, which
        ``_invalidate_reverted_prompt_if_binding_invalid()`` rightly refuses -
        and the exact rollback collapsed to ``draft`` purely because of that
        technical gap.

        The gap is closable without weakening anything, because the selected
        revision *is* the missing value: it provably contains this prompt's
        root version, and the fingerprint it restored still describes the
        content it restored. Both facts are checked here with the existing
        central primitives - never a re-derived membership, payload, hashing
        or validation rule.

        Returns ``True`` only when every one of these holds:

        1. a target revision id was carried through the request-local state;
        2. the restored row really is ``review``;
        3. no contradictory approval binding is present;
        4. the revision row still exists on this alias;
        5. it contains a root version of exactly this prompt;
        6. the canonical payload of the restored content fingerprints to the
           restored ``review_payload_fingerprint`` - obtained through this
           admin's one sanctioned ``_live_review_payload_fingerprint()`` access
           point, i.e. bit-for-bit the value the stale check below judges by
           (an empty or malformed stored value can never equal a freshly
           computed lowercase digest, so this subsumes the format check - which
           the central validator in step 8 then re-applies explicitly anyway);
        7. writing ``review_revision`` succeeds;
        8. ``validate_review_binding()`` accepts the result.

        Anything else returns ``False`` and the caller falls back to the
        existing fail-closed path, which clears the binding and lands the row
        in ``draft`` - so no partial binding can survive, not even the write
        in step 7.

        ``approved_revision`` is never set here: a review binding has none by
        contract, and step 3 refuses to reconstruct on top of one.

        Only ``Revision.DoesNotExist`` is caught, and only because a dangling
        target is an ordinary "not provable" outcome. ``IntegrityError``,
        ``ProgrammingError``, alias/routing problems and every other
        unexpected error propagate and roll the whole revert transaction back.
        No new revision is created: this runs inside ``save_related()``, i.e.
        inside the single ``reversion.create_revision()`` block VersionAdmin
        already opened.
        """
        if revision_id is None:
            return False

        current = Prompt._default_manager.using(using).get(pk=instance.pk)

        if current.status != EditorialWorkflowMixin.STATUS_REVIEW:
            return False
        if current.approved_revision_id is not None:
            return False

        try:
            revision = Revision.objects.using(using).get(pk=revision_id)
        except Revision.DoesNotExist:
            return False

        if not revision_contains_object(revision, current, using=using):
            return False

        live_fingerprint = self._live_review_payload_fingerprint(current, using=using)
        if current.review_payload_fingerprint != live_fingerprint:
            return False

        current.review_revision = revision
        current.save(update_fields=["review_revision"])

        return validate_review_binding(current, using=using).is_valid

    def _invalidate_reverted_prompt_if_binding_invalid(self, instance, *, using):
        """
        Beta 11.11C4J: the payload-unchanged half of the revert product
        decision. A reversion revert restores *every* local field on the
        Prompt row, including ``status``, ``review_revision``,
        ``approved_revision`` and ``review_payload_fingerprint`` themselves
        (Prompt is fully reversion-registered, see
        ``core/reversion_registration.py``) - so even when the canonical
        payload itself did not change, the restored binding fields could be
        structurally broken (dangling/foreign revision reference, mismatched
        ``approved_revision``/``review_revision``, malformed fingerprint) or
        simply stale relative to the current live payload (the restored
        fingerprint reflects a *different* historical review moment).

        Only ``review``/``approved`` are ever considered - every other status
        is left alone, mirroring B2B2's own ``_INVALIDATABLE_STATUSES``
        contract rather than re-deriving it. Reuses exactly the existing
        public primitives: ``core.review_binding.validate_review_binding``/
        ``validate_approved_binding`` for structural validity, and - since Beta
        11.11D3C1 - this admin's one sanctioned
        ``_live_review_payload_fingerprint()`` access point to the
        already-canonical builder/fingerprint pair for the live comparison
        value - never a new binding or hashing implementation. Invalidates via the
        existing Beta 11.11B2B2 ``invalidate_editorial_review_state()``
        exactly once, and only when actually necessary; returns ``None`` when
        the restored binding is already valid and current, leaving it
        untouched.
        """
        current = Prompt._default_manager.using(using).get(pk=instance.pk)

        if current.status not in (
            EditorialWorkflowMixin.STATUS_REVIEW,
            EditorialWorkflowMixin.STATUS_APPROVED,
        ):
            return None

        if current.status == EditorialWorkflowMixin.STATUS_APPROVED:
            validation = validate_approved_binding(current, using=using)
        else:
            validation = validate_review_binding(current, using=using)

        if not validation.is_valid:
            return invalidate_editorial_review_state(current, using=using)

        live_fingerprint = self._live_review_payload_fingerprint(current, using=using)
        if current.review_payload_fingerprint == live_fingerprint:
            return None

        return invalidate_editorial_review_state(current, using=using)

    # ------------------------------------------------------------------
    # Beta 11.11C4J: django-reversion "Revert to this version" / "Recover"
    # ------------------------------------------------------------------
    #
    # ``reversion.admin.VersionAdmin._reversion_revisionform_view()`` (which
    # both ``revision_view()`` and ``recover_view()`` delegate to) runs, in
    # this fixed order, inside one outer ``transaction.atomic(using=version.db)``:
    #
    #     version.revision.revert(delete=True)      # <- real DB mutation, HERE
    #     with self.create_revision(request):
    #         response = self.changeform_view(...)  # -> save_model()/save_related()
    #
    # The historical row (root Prompt *and* its translations - Prompt is
    # reversion-registered with ``follow=("translations",)``, see
    # ``core/reversion_registration.py``) is restored via ``revert()`` -
    # BEFORE ``changeform_view()`` ever reaches ``save_model()``. A baseline
    # captured inside ``save_model()`` would therefore already see the
    # reverted content, not the state right before the click - the compare
    # in ``save_related()`` would then structurally never detect a change.
    #
    # C4J closes this by capturing the Beta 11.11C4G baseline itself, in an
    # outer ``transaction.atomic()`` that *wraps* the entire
    # ``super().revision_view()``/``super().recover_view()`` call - so
    # reversion's own inner atomic becomes a nested savepoint - and by
    # placing that baseline (or, for recovery, a baseline-less marker) on the
    # existing C4H request-local store *before* calling ``super()``. This
    # admin opens no ``reversion.create_revision()`` of its own anywhere in
    # this file (see ``StaticSafetyTests`` in
    # ``prompts/tests/test_admin_review_edit_guard.py``); the only revision
    # created for a real revert/recover is VersionAdmin's own, and any B2B2
    # save this guard triggers is recorded into that same, already-open
    # revision because it happens inside ``save_related()``, itself called
    # from inside VersionAdmin's ``with self.create_revision(request):``
    # block.

    def _get_prompt_version_or_404(self, *, version_id, object_id=None):
        """
        The same ``reversion.models.Version`` lookup ``VersionAdmin`` itself
        performs (``get_object_or_404(Version, pk=version_id, ...)``), with
        one addition ``VersionAdmin`` does not make on its own: the version's
        ``content_type`` must resolve to this admin's own concrete model.
        Neither ``revision_view()`` nor (especially) ``recover_view()``
        otherwise verify that a ``version_id`` reachable through
        ``admin:prompts_prompt_...`` actually belongs to a Prompt - without
        this check, a crafted ``version_id`` for an unrelated model could
        reach ``changeform_view(request, quote(version.object_id), ...)``
        with a numeric id that only accidentally matches a real Prompt pk.
        For ``revision_view`` the caller also passes ``object_id`` so the
        lookup enforces, in one query, that the version genuinely belongs to
        *that* requested object - never merely to *some* Prompt.
        """
        model_meta = Prompt._meta.concrete_model._meta
        qs = Version.objects.filter(
            pk=version_id,
            content_type__app_label=model_meta.app_label,
            content_type__model=model_meta.model_name,
        )
        if object_id is not None:
            qs = qs.filter(object_id=unquote(object_id))
        return get_object_or_404(qs)

    def _resolve_prompt_reversion_db_alias(self, version):
        """
        ``version.db`` is the alias ``VersionAdmin`` itself will use for its
        own ``transaction.atomic(using=version.db)`` - never a hardcoded
        ``"default"``, never sourced from request data. Validated against
        Django's real configured aliases before this admin opens its own
        outer atomic block on it.
        """
        db_alias = version.db
        if not db_alias or db_alias not in connections:
            raise Http404("This version has no valid database alias.")
        return db_alias

    def revision_view(self, request, object_id, version_id, extra_context=None):
        """
        Beta 11.11C4J: capture the Beta 11.11C4G payload baseline for this
        Prompt *before* ``version.revision.revert()`` can run inside
        ``super().revision_view()`` - see the module note above.

        Covers both request methods VersionAdmin itself handles identically
        here: a GET preview (reverts inside reversion's own atomic block,
        renders the reverted state, then rolls everything back via its
        internal ``_RollBackRevisionView`` - ``save_model()``/``save_related()``
        are never reached for a GET at all) and a confirmed POST (reverts,
        then runs the real ``changeform_view()`` save path). Either way the
        state placed below is removed in ``finally`` - consumed by
        ``save_related()`` on a successful save, or simply never consumed and
        discarded otherwise (invalid form, GET preview, or any exception).
        """
        version = self._get_prompt_version_or_404(version_id=version_id, object_id=object_id)
        db_alias = self._resolve_prompt_reversion_db_alias(version)
        prompt_id = int(version.object_id)
        store = _prompt_review_edit_baselines(request)

        with transaction.atomic(using=db_alias):
            baseline = capture_prompt_review_edit_baseline(
                Prompt(pk=prompt_id), using=db_alias
            )
            store[prompt_id] = _PromptReversionGuardState(
                source=_PromptReversionGuardSource.REVISION_REVERT,
                prompt_id=prompt_id,
                database_alias=db_alias,
                baseline=baseline,
                # Beta 11.11D3C: the selected revision, straight off the
                # already-verified Version - never re-derived from the URL in
                # the final hook, never "the newest revision".
                target_revision_id=version.revision_id,
            )
            try:
                return super().revision_view(request, object_id, version_id, extra_context)
            finally:
                store.pop(prompt_id, None)

    def recover_view(self, request, version_id, extra_context=None):
        """
        Beta 11.11C4J: place a baseline-less
        :data:`_PromptReversionGuardSource.RECOVERY` marker for this Prompt
        *before* ``super().recover_view()`` can run ``version.revision.revert()``.

        No baseline is captured here - the row does not exist yet, so there
        is nothing to read (``capture_prompt_review_edit_baseline()`` is
        never called for recovery). ``save_related()`` fail-closed invalidates
        a ``review``/``approved`` outcome unconditionally instead - see
        ``_finish_prompt_review_guard()``.
        """
        version = self._get_prompt_version_or_404(version_id=version_id)
        db_alias = self._resolve_prompt_reversion_db_alias(version)
        prompt_id = int(version.object_id)
        store = _prompt_review_edit_baselines(request)

        with transaction.atomic(using=db_alias):
            store[prompt_id] = _PromptReversionGuardState(
                source=_PromptReversionGuardSource.RECOVERY,
                prompt_id=prompt_id,
                database_alias=db_alias,
                baseline=None,
            )
            try:
                return super().recover_view(request, version_id, extra_context)
            finally:
                store.pop(prompt_id, None)

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))

        can_edit = self.has_change_permission(request, obj)

        if not can_edit:
            fields += ["intro", "body", "outro"]

        return fields

    def intro(self, obj):
        value = obj.safe_translation_getter("intro", any_language=True)
        return richtext(value or "")

    def body(self, obj):
        value = obj.safe_translation_getter("body", any_language=True)
        return richtext(value or "")

    def outro(self, obj):
        value = obj.safe_translation_getter("outro", any_language=True)
        return richtext(value or "")

    def get_prepopulated_fields(self, request, obj=None):
        return {"slug": ("title",)}

    def published_fmt(self, obj):
        if not obj.published_at:
            return "-"
        return date_format(obj.published_at, format="d.m.Y H:i", use_l10n=True)

    def updated_fmt(self, obj):
        if not obj.updated_at:
            return "-"
        return date_format(obj.updated_at, format="d.m.Y H:i", use_l10n=True)

    @admin.display(ordering="translations__title", description=_("Title"))
    def title_col(self, obj):
        return obj.safe_translation_getter("title", any_language=True) or f"Prompt #{obj.pk}"

    def get_urls(self):
        base_urls = super().get_urls()
        custom = [
            path("<path:object_id>/diff/", self.admin_site.admin_view(self.diff_view),
                 name="prompts_prompt_diff"),
            # Beta 11.5: saved-draft preview, mirroring guides_guide_draft_preview
            # (Beta 11.4). admin_site.admin_view() supplies both the staff
            # gate and never_cache (cacheable=False is its default), so the
            # response can never enter a shared cache; the object-level role
            # check happens inside the view itself.
            path(
                "<path:object_id>/preview/<str:language_code>/",
                self.admin_site.admin_view(self.draft_preview_view),
                name="prompts_prompt_draft_preview",
            ),
        ]
        return custom + base_urls

    def draft_preview_view(self, request, object_id, language_code, *args, **kwargs):
        """
        Render one saved prompt draft through the real public detail
        template, in one explicitly requested language.

        Read-only by construction: it resolves the object, builds a context
        and renders. Nothing here saves, transitions the FSM, writes a
        revision or touches ``live_i18n``.

        Permission is the existing object-level editorial contract
        (``EditorialWorkflowAdminMixin.has_change_permission``): Editor/Admin/
        superuser for any prompt, Author for their own only. Everything that
        fails - unknown id, unsupported language, missing translation, or a
        prompt the requester may not preview - answers with the same 404, so
        a non-owning author cannot use the endpoint to confirm that a given
        prompt id exists (deliberately not 403, which would leak exactly that).
        """
        if request.method not in ("GET", "HEAD"):
            return HttpResponseNotAllowed(["GET", "HEAD"])

        if not is_supported_preview_language(language_code):
            raise Http404("Unsupported preview language.")

        prompt = self.get_object(request, object_id)
        if prompt is None or not self.has_change_permission(request, prompt):
            raise Http404("Prompt not found.")

        # Fail closed: no fallback language, no any_language=True, and only a
        # genuinely stored translation counts (see has_saved_translation).
        if not has_saved_translation(prompt, language_code):
            raise Http404("Prompt has no saved translation in this language.")

        # The override covers context building *and* rendering, so nav,
        # breadcrumbs and every {% trans %} resolve in the previewed language;
        # it is scoped, so the ambient language is restored afterwards.
        with translation.override(language_code):
            context = build_draft_prompt_context(prompt, language_code)
            response = render(request, "prompts/prompt_detail.html", context)

        return apply_editorial_preview_headers(response, language_code)

    def render_change_form(self, request, context, add=False, change=False, form_url="", obj=None):
        """
        Expose the draft-preview link to the change form.

        The language is the tab Parler currently shows (``get_form_language``),
        never an ambient browser language, and the link is only offered when
        that language actually has a stored translation - otherwise it would
        point at a URL that fail-closes with a 404.
        """
        if obj is not None and obj.pk:
            language_code = self.get_form_language(request, obj)
            context["draft_preview_language"] = language_code
            context["show_draft_preview"] = bool(
                self.has_change_permission(request, obj)
                and has_saved_translation(obj, language_code)
            )
        return super().render_change_form(
            request, context, add=add, change=change, form_url=form_url, obj=obj
        )

    def diff_view(self, request, object_id, *args, **kwargs):
        obj = self.get_object(request, object_id)
        live_keys = set((obj.live_i18n or {}).keys()) if hasattr(obj, "live_i18n") else set()
        obj_langs = set(getattr(obj, "get_available_languages", lambda: [])())  # Parler
        project_langs = {code for code, _ in getattr(settings, "LANGUAGES", [])}
        langs = []
        for code in list(project_langs) + list(obj_langs) + list(live_keys):
            if code and code not in langs:
                langs.append(code)
        if not langs:
            langs = [get_language()]

        comparisons = []
        for lang in langs:
            with switch_language(obj, lang):
                left = {
                    "slug": obj.safe_translation_getter("slug"),
                    "public_slug": obj.safe_translation_getter("public_slug"),
                    "title": obj.safe_translation_getter("title"),
                    "intro": obj.safe_translation_getter("intro"),
                    "body": obj.safe_translation_getter("body"),
                    "outro": obj.safe_translation_getter("outro"),
                }

            live = get_live_display_instance(obj, lang)
            with switch_language(obj, lang):
                right = {
                    "slug": getattr(live, "slug", None),
                    "public_slug": getattr(live, "public_slug", None),
                    "title": getattr(live, "title", None),
                    "intro": getattr(live, "intro", None),
                    "body": getattr(live, "body", None),
                    "outro": getattr(live, "outro", None),
                }

            changes = build_field_diffs(left, right)
            if not changes:
                continue

            info = get_language_info(lang)
            comparisons.append({
                "code": lang,
                "name": info.get("name_local") or info.get("name") or lang,
                "changes": changes,
            })

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "object": obj,
            "comparisons": comparisons,
        }
        return TemplateResponse(request, "admin/prompts/prompt_diff.html", context)
