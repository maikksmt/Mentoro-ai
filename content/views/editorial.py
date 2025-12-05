from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django_fsm import can_proceed

from compare.models import Comparison
from content.decorators import require_group
from content.forms_editorial import SubmitToReviewForm, ReviewUpdateForm
from core.models.editorial import EditorialWorkflowMixin
from guides.models import Guide
from prompts.models import Prompt
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


def get_editorial_model(key: str):
    try:
        return EDITORIAL_MODEL_REGISTRY[key]
    except KeyError:
        raise Http404("Unknown editorial model")


def is_author(user, obj) -> bool:
    return getattr(obj, "author_id", None) == user.id


def is_editor(user) -> bool:
    return user.groups.filter(name__in=["Editor", "Admin"]).exists()


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
    if not hasattr(obj, method_name):
        messages.error(request, _("Transition not available on this object."))
        return redirect("content:editorial:my_content")
    if not can_proceed(getattr(obj, method_name)):
        messages.error(request, _("Transition not allowed from current state."))
        return redirect("content:editorial:my_content")
    try:
        getattr(obj, method_name)(by=request.user)
        obj.save()
        messages.success(request, _("Status updated."))
    except Exception as e:
        messages.error(request, _(f"Could not change status: {e}"))

    return redirect("content:editorial:my_content")


@require_group("Author", "Editor", "Admin")
@login_required
def my_content_update(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return redirect("content:editorial:my_content")

    form = ReviewUpdateForm(request.POST, user=request.user)
    if not form.is_valid():
        messages.error(request, _("Invalid request."))
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

    if not hasattr(obj, method_name):
        messages.error(request, _("Transition not available on this object."))
        return redirect("content:editorial:my_content")

    transition = getattr(obj, method_name)
    if not can_proceed(transition):
        messages.error(request, _("Transition not allowed from current state."))
        return redirect("content:editorial:my_content")

    try:
        transition(by=request.user)
        obj.save()
        messages.success(request, _("Status updated."))
    except Exception as e:
        messages.error(request, _(f"Could not change status: {e}"))

    return redirect("content:editorial:my_content")


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
        messages.error(request, _("Invalid request."))
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

    if not hasattr(obj, method_name):
        messages.error(request, _("Transition not available on this object."))
        return redirect("content:editorial:review_queue")
    if not can_proceed(getattr(obj, method_name)):
        messages.error(request, _("Transition not allowed from current state."))
        return redirect("content:editorial:review_queue")
    try:
        getattr(obj, method_name)(by=request.user)
        obj.save()
        messages.success(request, _("Status updated."))
    except Exception as e:
        messages.error(request, _(f"Could not change status: {e}"))
    return redirect("content:editorial:review_queue")
