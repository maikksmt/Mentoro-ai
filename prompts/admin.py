# prompts/admin.py
from django.conf import settings
from django.contrib import admin, messages
from django.http import Http404, HttpResponseNotAllowed
from django.shortcuts import render
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

from content.templatetags.richtext import richtext
from core.admin import TranslatableTinyMCEMixin, EditorialWorkflowAdminMixin
from core.editorial_preview import (
    apply_editorial_preview_headers,
    has_saved_translation,
    is_supported_preview_language,
)
from core.services import get_live_display_instance, build_field_diffs
from .models import Prompt
from .presentation import build_draft_prompt_context
from .review_approval import (
    PromptReviewApprovalError,
    PromptReviewApprovalErrorCode,
    approve_prompt_review,
)
from .review_submission import (
    PromptReviewSubmissionError,
    PromptReviewSubmissionErrorCode,
    submit_prompt_for_review,
)


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
#: primitive (Beta 11.11C2A / C3A) that opens its own atomic transaction and
#: reversion revision per object. Extended in Beta 11.11C3B to cover approval
#: alongside submission - see ``PromptAdmin.changelist_view``.
_ISOLATED_PROMPT_ACTIONS = frozenset(
    {
        "action_submit_for_review",
        "action_approve",
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
        Run the prompt submit-for-review and approve actions *outside*
        VersionAdmin's shared revision context (Beta 11.11C2B, hardened in
        Beta 11.11C2B1A, extended to approval in Beta 11.11C3B).

        ``reversion.admin.VersionAdmin.changelist_view`` wraps the entire
        changelist POST - including admin-action dispatch - in one
        ``reversion.create_revision()`` block. That is exactly what the shared
        editorial submit/approve actions relied on to batch every selected
        object into a single revision. This admin's submit and approve actions
        instead delegate each prompt to ``submit_prompt_for_review`` (Beta
        11.11C2A) or ``approve_prompt_review`` (Beta 11.11C3A), each of which
        opens its own per-root revision and, by design, refuses to run inside
        an outer reversion context. So for either of those two actions
        (:data:`_ISOLATED_PROMPT_ACTIONS`) we dispatch through
        ``ModelAdmin.changelist_view`` (``super(VersionAdmin, self)``),
        skipping only VersionAdmin's revision wrapper; every other request -
        the GET changelist, and the rework/publish/archive/restore actions
        that still write directly inside a revision - keeps VersionAdmin's
        normal behaviour.

        Which action was actually selected is decided by
        ``_selected_action_name``, not by a bare ``request.POST.get("action")``
        - a changelist POST can carry more than one ``action`` field (top and
        bottom action bars), and only the one ``index`` actually points at
        counts. See that function's docstring for the exact contract,
        including its fail-closed behaviour for a negative or out-of-range
        index. That contract is reused verbatim here - only the set of action
        names it is compared against grew from one to two.
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
