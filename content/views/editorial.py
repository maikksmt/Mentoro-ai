from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import DEFAULT_DB_ALIAS
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _

from compare.models import Comparison
from content.decorators import require_group
from content.forms_editorial import REWORK_STATUS, ReviewUpdateForm, SubmitToReviewForm
from core.editorial_actions import (
    EditorialAction,
    EditorialActionError,
    EditorialActionErrorCode,
    apply_editorial_action,
)
from core.models.editorial import EditorialWorkflowMixin
from guides.models import Guide
from prompts.models import Prompt
from prompts.review_approval import (
    PromptReviewApprovalError,
    PromptReviewApprovalErrorCode,
    approve_prompt_review,
)
from prompts.review_publish import (
    PromptReviewPublishError,
    PromptReviewPublishErrorCode,
    publish_prompt_review,
)
from prompts.review_submission import (
    PromptReviewSubmissionError,
    PromptReviewSubmissionErrorCode,
    submit_prompt_for_review,
)
from usecases.models import UseCase

EDITORIAL_MODEL_REGISTRY: dict[str, type] = {
    "guide": Guide,
    "prompt": Prompt,
    "usecase": UseCase,
    "comparison": Comparison,
}

STATUS_TRANSITIONS: dict[str, tuple[str, str]] = {
    # target_state: (method_name, permission_codename)
    "review": ("move_to_review", "content.submit_for_review"),
    "rework": ("request_rework", "content.request_rework"),
    "approved": ("approve", "content.approve"),
    "published": ("publish", "content.publish"),
    "archived": ("archive", "content.archive"),
    "draft": ("restore", "content.restore"),
}

#: Target status -> the shared, surface-independent action that performs it
#: (Beta 11.13D1B). Before D1B this module ran the FSM transition and
#: ``obj.save()`` itself, which produced no reversion revision, no revision
#: user, no audit comment and - for Guide/Use Case/Comparison - no
#: ``last_published_revision_id``, while the admin's equivalent action produced
#: all four. Both surfaces now delegate to ``core.editorial_actions``, so the
#: same editorial action leaves the same persistent state whichever surface it
#: was triggered from.
#:
#: Prompt's ``review``/``approved``/``published`` never reach this table: they
#: are intercepted above by the C2A/C3A/D2 primitives, which the shared action
#: refuses to shadow.
EDITORIAL_ACTIONS: dict[str, EditorialAction] = {
    "review": EditorialAction.SUBMIT_FOR_REVIEW,
    "rework": EditorialAction.REQUEST_REWORK,
    "approved": EditorialAction.APPROVE,
    "published": EditorialAction.PUBLISH,
    "archived": EditorialAction.ARCHIVE,
    "draft": EditorialAction.RESTORE_TO_DRAFT,
}


def _review_form_error(form) -> str | None:
    """
    The first ``review_note`` error, if that is why the form failed.

    Beta 11.13D1G-a: a missing rework reason is a normal editorial mistake and
    deserves to say so, while every other invalid payload keeps the existing
    generic wording - those are malformed requests, not user errors worth
    describing in detail.
    """
    errors = form.errors.get("review_note")
    return errors[0] if errors else None


def _apply_shared_editorial_action(request, obj, target_status: str, note: str | None = None) -> None:
    """
    Run one workflow action through the shared primitive and render this
    surface's existing messages for it.

    ``note`` is only ever passed for rework (Beta 11.13D1G-a); every other
    action leaves it ``None`` so the primitive keeps writing its own canonical
    note and stays byte-identical to the admin path.

    Only ``STATUS_NOT_ELIGIBLE``/``TRANSITION_UNAVAILABLE`` are routine
    editorial outcomes and reuse the wording this view already had. Everything
    else is an integrity failure and is left to propagate - never flattened
    into a status message that would tell an author their change had been
    saved when it had not.
    """
    action = EDITORIAL_ACTIONS[target_status]
    try:
        apply_editorial_action(obj, action, actor=request.user, note=note)
    except EditorialActionError as exc:
        if exc.code in (
            EditorialActionErrorCode.STATUS_NOT_ELIGIBLE,
            EditorialActionErrorCode.TRANSITION_UNAVAILABLE,
        ):
            messages.error(request, _("Transition not allowed from current state."))
            return
        raise
    messages.success(request, _("Status updated."))


def get_editorial_model(key: str):
    try:
        return EDITORIAL_MODEL_REGISTRY[key]
    except KeyError:
        raise Http404("Unknown editorial model")


def is_author(user, obj) -> bool:
    return getattr(obj, "author_id", None) == user.id


def is_editor(user) -> bool:
    return user.groups.filter(name__in=["Editor", "Admin"]).exists()


def _is_concrete_prompt(obj) -> bool:
    """
    Beta 11.11C4B: a proxy of Prompt counts, a Prompt subclass does not -
    mirrors the same ``_meta.concrete_model`` check the review-binding
    primitives themselves use, never a class-name string or duck-typing.
    """
    return obj._meta.concrete_model is Prompt


def _prompt_database_alias(obj) -> str:
    return obj._state.db or DEFAULT_DB_ALIAS


def _submit_prompt_for_review_via_primitive(request, obj: Prompt) -> None:
    """
    Beta 11.11C4B: routes a Prompt's "review" transition through the atomic,
    per-root ``submit_prompt_for_review()`` primitive (Beta 11.11C2A) instead
    of the generic ``move_to_review()`` + ``obj.save()`` path every other
    editorial type in this module still uses. No transaction, reversion
    context, or second save is opened here - the primitive owns all of that.

    ``STATUS_NOT_SUBMITTABLE`` reuses this view's existing "wrong state"
    message; ``OBJECT_NOT_FOUND`` reuses its existing 404 contract. Every
    other code (alias/actor/reversion/revision-graph/payload-consistency
    errors) is not a routine workflow outcome and is left to propagate -
    never disguised as a harmless status message.
    """
    try:
        submit_prompt_for_review(obj, actor=request.user, using=_prompt_database_alias(obj))
    except PromptReviewSubmissionError as exc:
        if exc.code == PromptReviewSubmissionErrorCode.STATUS_NOT_SUBMITTABLE:
            messages.error(request, _("Transition not allowed from current state."))
            return
        if exc.code == PromptReviewSubmissionErrorCode.OBJECT_NOT_FOUND:
            raise Http404("Prompt not found.") from exc
        raise
    messages.success(request, _("Status updated."))


def _approve_prompt_review_via_primitive(request, obj: Prompt) -> None:
    """
    Beta 11.11C4B: routes a Prompt's "approved" transition through the
    atomic, per-root ``approve_prompt_review()`` primitive (Beta 11.11C3A)
    instead of the generic ``approve()`` + ``obj.save()`` path. No
    transaction, reversion context, or second save is opened here.

    ``STATUS_NOT_APPROVABLE`` reuses the existing "wrong state" message,
    ``OBJECT_NOT_FOUND`` the existing 404 contract, and
    ``REVIEW_PAYLOAD_CHANGED`` gets its own message - the reviewed content
    changed since submit, so the prompt stays in review and must be
    resubmitted; this is never reported as "already moved back to draft/
    rework" since C3A itself performs no such invalidation. Everything else
    (``REVIEW_BINDING_INVALID``, ``ACTIVE_REVERSION_CONTEXT``,
    ``PAYLOAD_CHANGED_DURING_APPROVAL``, alias/actor errors) is a technical
    integrity failure, never a harmless-looking workflow skip, and is left to
    propagate.
    """
    try:
        approve_prompt_review(obj, actor=request.user, using=_prompt_database_alias(obj))
    except PromptReviewApprovalError as exc:
        if exc.code == PromptReviewApprovalErrorCode.STATUS_NOT_APPROVABLE:
            messages.error(request, _("Transition not allowed from current state."))
            return
        if exc.code == PromptReviewApprovalErrorCode.OBJECT_NOT_FOUND:
            raise Http404("Prompt not found.") from exc
        if exc.code == PromptReviewApprovalErrorCode.REVIEW_PAYLOAD_CHANGED:
            messages.error(
                request,
                _(
                    "The reviewed prompt content has changed. Submit it for "
                    "review again before approval."
                ),
            )
            return
        raise
    messages.success(request, _("Status updated."))


def _publish_prompt_review_via_primitive(request, obj: Prompt) -> None:
    """
    Beta 11.11D2: routes a Prompt's "published" transition through the
    atomic, per-root ``publish_prompt_review()`` primitive instead of the
    generic ``publish()`` + ``obj.save()`` path this module still uses for
    every other editorial type.

    That generic path was the weaker of the two publish routes: no
    transaction of its own, no reversion revision at all, no
    ``last_published_revision_id`` and - decisively - no check that the
    approval binding or the approved fingerprint still described the content
    being published. D2 owns all of that, so nothing is opened or saved here.

    ``STATUS_NOT_PUBLISHABLE`` reuses this view's existing "wrong state"
    message and ``OBJECT_NOT_FOUND`` its existing 404 contract, exactly as the
    submit and approve delegations do. ``REVIEW_PAYLOAD_CHANGED`` and the two
    translation-completeness codes get their own messages: all three are real
    editorial states an author can act on, not technical failures. Everything
    else (``REVIEW_BINDING_INVALID``, ``APPROVED_BINDING_INVALID``,
    ``ACTIVE_REVERSION_CONTEXT``, missing/ambiguous root version, failed
    postconditions, alias/actor errors) is an integrity failure and is left to
    propagate - never disguised as a harmless status message.
    """
    try:
        publish_prompt_review(obj, actor=request.user, using=_prompt_database_alias(obj))
    except PromptReviewPublishError as exc:
        if exc.code == PromptReviewPublishErrorCode.STATUS_NOT_PUBLISHABLE:
            messages.error(request, _("Transition not allowed from current state."))
            return
        if exc.code == PromptReviewPublishErrorCode.OBJECT_NOT_FOUND:
            raise Http404("Prompt not found.") from exc
        if exc.code == PromptReviewPublishErrorCode.PERMISSION_DENIED:
            messages.error(
                request, _("You are not allowed to perform this transition.")
            )
            return
        if exc.code == PromptReviewPublishErrorCode.REVIEW_PAYLOAD_CHANGED:
            messages.error(
                request,
                _(
                    "The approved prompt content has changed. Submit it for "
                    "review again before publishing."
                ),
            )
            return
        if exc.code in (
            PromptReviewPublishErrorCode.NO_PUBLISHABLE_TRANSLATION,
            PromptReviewPublishErrorCode.INCOMPLETE_TRANSLATION,
        ):
            messages.error(
                request,
                _(
                    "This prompt has no complete translation to publish. Every "
                    "published language needs a title and a slug."
                ),
            )
            return
        raise
    messages.success(request, _("Status updated."))


@login_required
def my_content(request: HttpRequest) -> HttpResponse:
    user = request.user

    items: list[tuple[str, EditorialWorkflowMixin]] = []

    for key, model in EDITORIAL_MODEL_REGISTRY.items():
        qs = model.objects.all()

        # nur eigene Beiträge
        if hasattr(model, "author"):
            qs = qs.filter(author=user)
        elif hasattr(model, "created_by"):
            qs = qs.filter(created_by=user)

        qs = qs.filter(status__in=["draft", "rework", "review", "approved", "published", "archived", ])

        for obj in qs:
            items.append((key, obj))

    items.sort(key=lambda t: (t[1].status, getattr(t[1], "pk", 0)))

    context = {
        "seo_title": _("My Content - Guides, Prompts, Usecases, Comparisons"),
        "items": items,
    }

    return render(request, "content/editorial/my_content.html", context)


@require_group("Author", "Editor", "Admin")
@login_required
def submit_to_review(request):
    if request.method != "POST":
        return redirect("content:editorial:my_content")
    form = SubmitToReviewForm(request.POST, user=request.user)
    if not form.is_valid():
        messages.error(request, _("Invalid request."))
        return redirect("content:editorial:my_content")
    model_key = form.cleaned_data["model"]
    object_id = form.cleaned_data["object_id"]

    model = get_editorial_model(model_key)
    obj = get_object_or_404(model, pk=object_id)
    method_name, perm = STATUS_TRANSITIONS.get("review", (None, None))
    if not method_name:
        messages.error(request, _("Unknown/unsupported target status."))
        return redirect("content:editorial:my_content")
    if not request.user.has_perm(perm, obj):
        messages.error(request, _("You are not allowed to submit this item for review."))
        return redirect("content:editorial:my_content")

    if _is_concrete_prompt(obj):
        _submit_prompt_for_review_via_primitive(request, obj)
        return redirect("content:editorial:my_content")

    _apply_shared_editorial_action(request, obj, "review")
    return redirect("content:editorial:my_content")


@require_group("Author", "Editor", "Admin")
@login_required
def my_content_update(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return redirect("content:editorial:my_content")

    form = ReviewUpdateForm(request.POST, user=request.user)
    if not form.is_valid():
        messages.error(request, _review_form_error(form) or _("Invalid request."))
        return redirect("content:editorial:my_content")

    model_key = form.cleaned_data["model"]
    object_id = form.cleaned_data["object_id"]
    new_status = form.cleaned_data["status"]

    model = get_editorial_model(model_key)
    obj = get_object_or_404(model, pk=object_id)

    method_name, perm = STATUS_TRANSITIONS.get(new_status, (None, None))
    if not method_name:
        messages.error(request, _("Unknown/unsupported target status."))
        return redirect("content:editorial:my_content")

    if not request.user.has_perm(perm, obj):
        messages.error(request, _("You are not allowed to perform this transition."))
        return redirect("content:editorial:my_content")

    if _is_concrete_prompt(obj) and new_status == "review":
        _submit_prompt_for_review_via_primitive(request, obj)
        return redirect("content:editorial:my_content")
    if _is_concrete_prompt(obj) and new_status == "approved":
        _approve_prompt_review_via_primitive(request, obj)
        return redirect("content:editorial:my_content")
    if _is_concrete_prompt(obj) and new_status == "published":
        _publish_prompt_review_via_primitive(request, obj)
        return redirect("content:editorial:my_content")

    _apply_shared_editorial_action(
        request, obj, new_status, note=_rework_note(form, new_status)
    )
    return redirect("content:editorial:my_content")


def _rework_note(form, target_status: str) -> str | None:
    """The validated reason, but only for the action that carries one."""
    if target_status != REWORK_STATUS:
        return None
    return form.cleaned_data.get("review_note") or None


@require_group("Editor", "Admin")
@login_required
def review_queue(request: HttpRequest) -> HttpResponse:
    queues: list[tuple[str, list]] = []

    for key, model in EDITORIAL_MODEL_REGISTRY.items():
        qs = model.objects.filter(status="review").select_related("author")
        queues.append((key, list(qs)))

    context = {
        "seo_title": "Review Queue",
        "queues": queues,
    }
    return render(request, "content/editorial/review_queue.html", context)


@require_group("Editor", "Admin")
@login_required
def review_update(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return redirect("content:editorial:review_queue")

    # POST
    form = ReviewUpdateForm(request.POST, user=request.user)
    if not form.is_valid():
        messages.error(request, _review_form_error(form) or _("Invalid request."))
        return redirect("content:editorial:review_queue")

    model_key = form.cleaned_data["model"]
    object_id = form.cleaned_data["object_id"]
    new_status = form.cleaned_data["status"]

    model = get_editorial_model(model_key)
    obj = get_object_or_404(model, pk=object_id)

    method_name, perm = STATUS_TRANSITIONS.get(new_status, (None, None))
    if not method_name:
        messages.error(request, _("Unknown/unsupported target status."))
        return redirect("content:editorial:review_queue")

    if not request.user.has_perm(perm, obj):
        messages.error(request, _("You are not allowed to perform this transition."))
        return redirect("content:editorial:review_queue")

    if _is_concrete_prompt(obj) and new_status == "review":
        _submit_prompt_for_review_via_primitive(request, obj)
        return redirect("content:editorial:review_queue")
    if _is_concrete_prompt(obj) and new_status == "approved":
        _approve_prompt_review_via_primitive(request, obj)
        return redirect("content:editorial:review_queue")
    if _is_concrete_prompt(obj) and new_status == "published":
        _publish_prompt_review_via_primitive(request, obj)
        return redirect("content:editorial:review_queue")

    _apply_shared_editorial_action(
        request, obj, new_status, note=_rework_note(form, new_status)
    )
    return redirect("content:editorial:review_queue")
