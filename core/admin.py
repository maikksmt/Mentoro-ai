from contextlib import contextmanager
from typing import ClassVar

import reversion
from django.conf import settings
from django.contrib import admin, messages
from django.db import IntegrityError, transaction
from django.utils.translation import gettext_lazy as _
from django_fsm import can_proceed
from parler.admin import TranslatableAdmin
from reversion.models import Version
from tinymce.widgets import TinyMCE

from core.editorial_actions import (
    EditorialAction,
    EditorialActionError,
    EditorialActionErrorCode,
    apply_editorial_action,
    publish_marker_scope,
)

#: The one :class:`~core.editorial_actions.EditorialActionError` outcome that is
#: an ordinary editorial result rather than a fault: the object simply is not in
#: a state this transition may start from. Every action below turns it into the
#: same "not executable" message it produced before Beta 11.13D1B. Any other
#: code is an integrity problem and is re-raised, never reported as a skip.
_NOT_EXECUTABLE_CODES = frozenset(
    {
        EditorialActionErrorCode.STATUS_NOT_ELIGIBLE,
        EditorialActionErrorCode.TRANSITION_UNAVAILABLE,
    }
)


def set_last_published_revision(obj):
    """
    Legacy publish-marker resolver.

    No workflow *action* uses this any more. Beta 11.13D1B moved all six of
    them to ``core.editorial_actions``, which resolves the marker from
    ``post_revision_commit`` - once reversion has written the revision's
    versions - by an exact ``(revision, content_type, object_id, db)`` lookup
    against the publish's own root version.

    This helper resolved it with an unordered
    ``Version.objects.get_for_object(obj).first()`` from *inside* the
    still-open revision block, i.e. before those versions existed, so it could
    only ever point at an older revision - or, on a first publish, at nothing
    at all.

    It remains in place for the one caller D1B deliberately does not touch:
    :meth:`EditorialWorkflowAdminMixin.save_model`, where it backfills a marker
    for a published row saved through the admin changeform rather than through
    a workflow action. Correcting that path is a separate concern from making
    the two *action* surfaces agree, and is left to its own slice.
    """
    latest = Version.objects.get_for_object(obj).first()
    if latest:
        obj.last_published_revision_id = latest.id


class TranslatableTinyMCEMixin(TranslatableAdmin):
    """
    Admin mixin for Parler models: swaps specified translation fields to TinyMCE,
    and loads wide-field CSS for better authoring UX.
    """
    tinymce_fields: tuple[str, ...] = ()

    class Media:
        css: ClassVar[dict[str, tuple[str, ...]]] = {
            'all': ('admin/custom/wide-fields.css',)
        }

    def get_form(self, request, obj=None, **kwargs):
        """
        Wraps the standard admin form and injects TinyMCE widgets for configured fields;
        also widens common title fields to improve readability.
        """
        form = super().get_form(request, obj, **kwargs)
        for name in self.tinymce_fields:
            if name in form.base_fields:
                form.base_fields[name].widget = TinyMCE(attrs={"rows": 18})
        if 'title' in form.base_fields:
            w = form.base_fields['title'].widget
            style = w.attrs.get('style', '')
            w.attrs['style'] = (style + '; width:60em').strip('; ')
            css_classes = w.attrs.get('class', '')
            if 'vTextField' not in css_classes.split():
                w.attrs['class'] = (css_classes + ' vTextField').strip()
        if 'slug' in form.base_fields:
            w = form.base_fields['slug'].widget
            style = w.attrs.get('style', '')
            w.attrs['style'] = (style + '; width:60em').strip('; ')
            css_classes = w.attrs.get('class', '')
            if 'vTextField' not in css_classes.split():
                w.attrs['class'] = (css_classes + ' vTextField').strip()

        return form

    def get_language_tabs(self, request, obj, available_languages, css_class=None):
        """
        Beta 11.11D3B: bind parler's per-language "delete translation" link to
        the same permission its own view already enforces.

        ``parler.utils.views.get_language_tabs()`` sets
        ``tabs.allow_deletion`` purely from
        ``len(available_languages) > 1`` - it consults no permission at all -
        while ``TranslatableAdmin.delete_translation()`` gates the actual
        deletion on ``has_delete_permission(request, translation)``. Any role
        without the model's ``delete_*`` permission (Author and Editor, see
        ``accounts/signals.py::ensure_editorial_groups``) was therefore
        offered a delete link on every language tab that answered 403 when
        followed.

        This narrows the *link* to the permission that already governs the
        *action*; it never widens it. ``allow_deletion`` stays false whenever
        parler already said so (a single remaining translation), and the
        server-side check in ``delete_translation()`` is untouched - so a
        direct URL is still refused independently of what is rendered.

        The permission is resolved through ``self.has_delete_permission()``,
        i.e. whichever implementation the concrete admin actually uses
        (``EditorialWorkflowAdminMixin``, ``ChildOfGuideOwnershipMixin``, or
        plain ``ModelAdmin``). No group name is hardcoded here, and Admin-group
        members and superusers keep the link exactly as before.
        """
        tabs = super().get_language_tabs(request, obj, available_languages, css_class=css_class)
        tabs.allow_deletion = tabs.allow_deletion and self.has_delete_permission(request, obj)
        return tabs


class TranslatableTinyMCEInlineMixin:
    """
    Inline admin counterpart that applies TinyMCE to translation fields inside inlines
    to keep the editing experience consistent.
    """
    tinymce_fields: tuple[str, ...] = ()
    wide_text_inputs: tuple[str, ...] = ("title", "label")

    class Media:
        css: ClassVar[dict[str, tuple[str, ...]]] = {
            'all': ('admin/custom/wide-fields.css',)
        }

    def get_formset(self, request, obj=None, **kwargs):
        """
        Overrides formset creation to attach TinyMCE widgets for inline translation fields.
        """
        formset = super().get_formset(request, obj, **kwargs)
        base_fields = formset.form.base_fields

        for name in self.tinymce_fields:
            if name in base_fields:
                base_fields[name].widget = TinyMCE(attrs={"rows": 14})

        # Gezielt Breite erhöhen (z. B. für title)
        for name in self.wide_text_inputs:
            if name in base_fields:
                w = base_fields[name].widget
                attrs = getattr(w, "attrs", {})
                attrs.update({"style": "min-width:40rem"})  # oder 60rem
                w.attrs = attrs

        return formset


class EditorialWorkflowAdminMixin(admin.ModelAdmin):
    """
    Common admin logic for all models that use EditorialWorkflowMixin:
    - Admin-Actions for FSM-Transitions (Draft/Review/Rework/Published/Archived)
    - Permissions via core.authz (rules)
    - automatic "review" transition for changes
    """
    AUTHOR_GROUP_NAMES = getattr(settings, "AUTHOR_GROUP_NAMES", ["Author"])
    EDITOR_GROUP_NAMES = getattr(settings, "EDITOR_GROUP_NAMES", ["Editor", "Admin"])
    ADMIN_GROUP_NAMES = getattr(settings, "ADMIN_GROUP_NAMES", ["Admin"])

    workflow_actions = (
        "action_submit_for_review",
        "action_request_rework",
        "action_approve",
        "action_publish",
        "action_archive",
        "action_restore_draft",
    )

    # Standard description for auto review note; can be overwritten per admin class
    auto_review_note = _("Auto: Change in the admin form")

    actions = workflow_actions

    #: Beta 11.11D3C: the rollback form gets an explicit warning that the
    #: current editing state - later translations, sections and entries
    #: included - is replaced and that the workflow/publication status is
    #: restored too. Only ``revision_form_template`` is redirected;
    #: ``recover_form_template`` stays at reversion's default because that
    #: wording would be wrong for a deleted object (nothing is replaced and
    #: no later work is removed).
    revision_form_template = "admin/editorial/revision_form.html"

    #: Beta 11.11D3C: request-local marker for "this request is inside
    #: django-reversion's revert/recover form". Set for the duration of the
    #: reversion view only, removed in ``finally``. Never a module global,
    #: never a ``ContextVar`` or thread-local, never persisted.
    _REVERSION_FORM_REQUEST_ATTR = "_mentoro_editorial_reversion_form"

    # ---- internal helper ----

    def _get_transition(self, obj, name: str):
        return getattr(obj, name, None)

    def changelist_view(self, request, extra_context=None):
        """
        Beta 11.13D1B1: bound the pending publish markers to this request.

        ``VersionAdmin.changelist_view`` wraps the whole changelist POST -
        admin-action dispatch included - in one ``reversion.create_revision()``,
        which is what keeps a bulk selection in a single shared revision. That
        revision therefore commits *after* the action has already returned, and
        ``post_revision_commit`` - the only thing that resolves a publish marker
        - fires at that moment. So the marker scope has to enclose
        ``VersionAdmin``'s context rather than sit inside the action, which is
        why it is opened here and not in ``action_publish``.

        Opening it around every changelist request (not only publishes) is
        deliberate: it costs one ``ContextVar`` set/reset, and it means the
        ``finally`` runs even when the revision is aborted by an action that
        raises after some objects were already published - the path that
        previously left an entry behind for the worker's next request.

        Purely an in-memory lifecycle boundary: no query, no permission, no
        message and no response behaviour changes here.
        """
        with publish_marker_scope():
            return super().changelist_view(request, extra_context)

    # ---- Beta 11.11D3C: reversion revert/recover form context ----

    def _is_reversion_form_request(self, request) -> bool:
        return bool(getattr(request, self._REVERSION_FORM_REQUEST_ATTR, False))

    @contextmanager
    def _reversion_form_request(self, request):
        setattr(request, self._REVERSION_FORM_REQUEST_ATTR, True)
        try:
            yield
        finally:
            try:
                delattr(request, self._REVERSION_FORM_REQUEST_ATTR)
            except AttributeError:  # pragma: no cover - defensive only
                pass

    def revision_view(self, request, object_id, version_id, extra_context=None):
        """
        Beta 11.11D3C: mark the request while django-reversion restores and
        re-saves the selected revision.

        ``VersionAdmin._reversion_revisionform_view()`` runs
        ``version.revision.revert(delete=True)`` and *then* the ordinary
        ``changeform_view()``. Without the marker, the shared auto-review
        below sees a ``published`` row plus a ``form.has_changed()`` that is
        true even for an unmodified confirmation (parler's translated-field
        initials lag the same-transaction write) and immediately moves the
        just-restored row to ``review`` - overwriting exactly the state the
        rollback was meant to reproduce.

        Resolved through ``super()``, which reaches ``VersionAdmin`` via the
        concrete admin's MRO; every editorial root admin is a ``VersionAdmin``.

        Deliberately *only* ``revision_view``. ``recover_view`` restores a
        previously hard-deleted object rather than replacing a working state;
        it keeps its pre-D3C contract, auto-review included, and therefore
        gets no override here at all.
        """
        with self._reversion_form_request(request):
            return super().revision_view(request, object_id, version_id, extra_context)

    def _user_has_perm(self, request, perm_codename: str, obj) -> bool:
        """
        Uses rules-based object permissions from core.authz: Content.<perm_codename>
        """
        return request.user.has_perm(f"content.{perm_codename}", obj)

    def _save_with_fields(self, obj, *field_names):
        fields = [f for f in field_names if hasattr(obj, f)]
        if fields:
            obj.save(update_fields=fields)
        else:
            obj.save()

    def _is_editor_user(self, user):
        if not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        return user.groups.filter(name__in=self.EDITOR_GROUP_NAMES).exists()

    def _is_author_of(self, user, obj):
        if not user.is_authenticated or obj is None:
            return False
        return getattr(obj, "author_id", None) == user.id

    def _is_author_user(self, user):
        if not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        return user.groups.filter(name__in=self.AUTHOR_GROUP_NAMES).exists()

    def _is_admin_user(self, user):
        if not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        return user.groups.filter(name__in=self.ADMIN_GROUP_NAMES).exists()

    # ---- Auto-Review-Logik (beim Speichern im Admin) ----

    def _must_auto_review(self, original_obj, form, formsets=None) -> bool:
        """
        Checks if you want to switch from PUBLISHED → REVIEW automatically,
        Because changes have happened in the form or inline form sets.
        """
        if not original_obj:
            return False

        if getattr(original_obj, "status", "") != getattr(original_obj, "STATUS_PUBLISHED", "published"):
            return False

        if form and form.has_changed():
            return True

        # Inline-Formsets (Sections, Items)
        for fs in (formsets or []):
            # Django-admin Inline API (m2m / inlines)
            if getattr(fs, "changed_objects", None) and fs.changed_objects:
                return True
            if getattr(fs, "new_objects", None) and fs.new_objects:
                return True
            if getattr(fs, "deleted_objects", None) and fs.deleted_objects:
                return True

            # Formset-API
            if getattr(fs, "changed_forms", None) and fs.changed_forms:
                return True
            if getattr(fs, "new_forms", None) and fs.new_forms:
                return True
            if getattr(fs, "deleted_forms", None) and fs.deleted_forms:
                return True

        return False

    def _auto_transition_to_review(self, request, obj):
        """
        Automatically moves an already published object to REVIEW,
        If the user is allowed and the transition is available.

        Beta 11.11D3C: never inside a reversion revert/recover form - there
        the selected revision is the source of truth for the workflow state,
        not a form-change signal (see :meth:`revision_view`).
        """
        if self._is_reversion_form_request(request):
            return

        if not self._user_has_perm(request, "submit_for_review", obj):
            return

        transition = self._get_transition(obj, "move_to_review")
        if transition and can_proceed(transition):
            try:
                transition(by=request.user, note=self.auto_review_note)
            except TypeError:
                transition()

            obj.save()

    # --- Object-related rights in the admin ---

    def has_change_permission(self, request, obj=None):

        if obj is None:
            # Changelist/module-level access: real editorial roles (or
            # Django's own change_<model> permission, e.g. for a future
            # non-group grant) - not just is_staff on its own, which used to
            # let any staff user reach this changelist regardless of role.
            return (
                self._is_editor_user(request.user)
                or self._is_author_user(request.user)
                or super().has_change_permission(request, obj)
            )

        if self._is_editor_user(request.user):
            return True

        return self._is_author_of(request.user, obj)

    def has_delete_permission(self, request, obj=None):

        base = super().has_delete_permission(request, obj)
        if not base:
            return False

        if obj is None:
            return base

        if self._is_editor_user(request.user):
            return True

        return self._is_author_of(request.user, obj)

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if not self._is_editor_user(request.user) and "author" in [f.name for f in self.model._meta.fields]:
            fields.append("author")

        if not self._is_admin_user(request.user) and "published_at" in [f.name for f in self.model._meta.fields]:
            fields.append("published_at")

        return fields

    # ---- Save-hook that all editorial admins share ----

    def save_model(self, request, obj, form, change):
        if not change and getattr(obj, "author_id", None) is None:
            obj.author = request.user

        original = None
        if change and obj.pk:
            try:
                original = self.model.objects.get(pk=obj.pk)
            except self.model.DoesNotExist:
                original = None

        if self._must_auto_review(original, form, formsets=None):
            self._auto_transition_to_review(request, obj)

        super().save_model(request, obj, form, change)

        if (getattr(obj, "status", None) == getattr(obj, "STATUS_PUBLISHED", "published")
                and not getattr(obj, "last_published_revision_id", None)):
            set_last_published_revision(obj)
            self._save_with_fields(obj, "last_published_revision_id")

    auto_inline_review_note = _("Auto: Change to inline content")

    # ---- save_related-hook that all editorial admins share ----

    def _inlines_changed(self, formsets) -> bool:
        """
        Checks if something has been changed, re-created or deleted in one of the inlines.
        """
        for fs in formsets:
            changed = any(
                getattr(f, "has_changed", lambda: False)()
                for f in getattr(fs, "forms", [])
            )

            new_forms = [
                f
                for f in getattr(fs, "forms", [])
                if not getattr(getattr(f, "instance", None), "pk", None)
                   and not getattr(getattr(f, "cleaned_data", {}), "get", lambda *_: False)("DELETE", False)
            ]

            deleted = bool(getattr(fs, "deleted_objects", []))

            if changed or new_forms or deleted:
                return True

        return False

    def save_related(self, request, form, formsets, change):
        """
        Central auto-review logic for changes to inlines
        for all editorial modeladmins.

        Beta 11.11D3C: skipped inside a reversion revert/recover form for the
        same reason as :meth:`_auto_transition_to_review` - restoring a
        revision that legitimately contains sections or entries must not read
        as "an editor just changed the inlines".
        """
        obj = form.instance
        inline_changed = self._inlines_changed(formsets)

        if (
            inline_changed
            and not self._is_reversion_form_request(request)
            and getattr(obj, "status", None) == getattr(obj, "STATUS_PUBLISHED", "published")
            and self._user_has_perm(request, "submit_for_review", obj)
        ):
            transition = self._get_transition(obj, "move_to_review")
            if transition and can_proceed(transition):
                try:
                    transition(
                        by=request.user,
                        note=self.auto_inline_review_note,
                    )
                except TypeError:
                    transition()
                self._save_with_fields(
                    obj,
                    "status",
                    "submitted_for_review_at",
                    "review_note",
                    "updated_at",
                )

        return super().save_related(request, form, formsets, change)

    # ---- Admin-Actions (Workflow) ----

    @admin.action(description=_("Send to review"))
    def action_submit_for_review(self, request, queryset):
        moved, skipped = 0, []

        with transaction.atomic(), reversion.create_revision():
            reversion.set_user(request.user)
            reversion.set_comment("submit_for_review")

            for obj in queryset.select_for_update():
                if not self._user_has_perm(request, "submit_for_review", obj):
                    skipped.append((obj.pk, _("permission denied")))
                    continue

                try:
                    apply_editorial_action(
                        obj, EditorialAction.SUBMIT_FOR_REVIEW, actor=request.user
                    )
                except EditorialActionError as exc:
                    if exc.code in _NOT_EXECUTABLE_CODES:
                        skipped.append(
                            (obj.pk, _("Transition 'move_to_review' not executable"))
                        )
                        continue
                    raise
                moved += 1

        if moved:
            self.message_user(
                request,
                _("%(n)d item(s) → Review.") % {"n": moved},
                level=messages.SUCCESS,
            )
        if skipped:
            detail = ", ".join([f"#{pk}: {reason}" for pk, reason in skipped])
            self.message_user(
                request,
                _("%(n)d skipped: %(detail)s") % {"n": len(skipped), "detail": detail},
                level=messages.WARNING,
            )

    @admin.action(description=_("Request changes (→ Rework)"))
    def action_request_rework(self, request, queryset):
        ok = 0
        note = request.POST.get("review_note", "")

        with reversion.create_revision():
            for obj in queryset:
                try:
                    if not self._user_has_perm(request, "request_rework", obj):
                        raise PermissionError(
                            _("You are not authorized to perform this action.")
                        )

                    try:
                        apply_editorial_action(
                            obj,
                            EditorialAction.REQUEST_REWORK,
                            actor=request.user,
                            note=note,
                        )
                    except EditorialActionError as exc:
                        if exc.code in _NOT_EXECUTABLE_CODES:
                            raise RuntimeError(
                                _("Transition 'request_rework' not executable")
                            ) from exc
                        raise
                    ok += 1
                except Exception as e:  # noqa: BLE001 - one bad row must not abort the whole bulk action
                    self.message_user(
                        request, f"{obj}: {e}", level=messages.ERROR
                    )

            reversion.set_user(request.user)
            reversion.set_comment("request_rework")

        self.message_user(
            request,
            _("%(n)d item(s) → Rework.") % {"n": ok},
            level=messages.SUCCESS,
        )

    @admin.action(description=_("Approve (Review → Approved)"))
    def action_approve(self, request, queryset):
        approved, skipped = 0, []

        with transaction.atomic(), reversion.create_revision():
            reversion.set_user(request.user)
            reversion.set_comment("approve")

            note = request.POST.get("review_note", "")

            for obj in queryset.select_for_update():
                if not self._user_has_perm(request, "approve", obj):
                    skipped.append((obj.pk, _("permission denied")))
                    continue

                try:
                    apply_editorial_action(
                        obj, EditorialAction.APPROVE, actor=request.user, note=note
                    )
                except EditorialActionError as exc:
                    if exc.code in _NOT_EXECUTABLE_CODES:
                        skipped.append(
                            (obj.pk, _("transition 'approve' not executable"))
                        )
                        continue
                    raise
                approved += 1

        if approved:
            self.message_user(
                request,
                _("%(n)d item(s) approved.") % {"n": approved},
                level=messages.SUCCESS,
            )

        if skipped:
            detail = ", ".join([f"#{pk}: {reason}" for pk, reason in skipped])
            self.message_user(
                request,
                _("%(n)d skipped: %(detail)s") % {"n": len(skipped), "detail": detail},
                level=messages.WARNING,
            )

    @admin.action(description=_("Publish selected item(s)"))
    def action_publish(self, request, queryset):
        published, skipped = 0, []

        with transaction.atomic(), reversion.create_revision():
            reversion.set_user(request.user)
            reversion.set_comment("Admin-Action: publish")

            for obj in queryset.select_for_update():
                if not self._user_has_perm(request, "publish", obj):
                    skipped.append((obj.pk, _("permission denied")))
                    continue

                if getattr(obj, "status", None) == getattr(obj, "STATUS_PUBLISHED", "published"):
                    continue

                try:
                    apply_editorial_action(
                        obj, EditorialAction.PUBLISH, actor=request.user
                    )
                except EditorialActionError as exc:
                    if exc.code in _NOT_EXECUTABLE_CODES:
                        skipped.append(
                            (obj.pk, _("Transition 'publish' not executable"))
                        )
                        continue
                    raise
                except IntegrityError as exc:
                    skipped.append((obj.pk, _("Could not publish: %(error)s") % {"error": exc}))
                    continue
                published += 1

        if published:
            self.message_user(
                request,
                _("%(n)d item(s) published.") % {"n": published},
                level=messages.SUCCESS,
            )
        if skipped:
            detail = ", ".join([f"#{pk}: {reason}" for pk, reason in skipped])
            self.message_user(
                request,
                _("%(n)d skipped: %(detail)s") % {"n": len(skipped), "detail": detail},
                level=messages.WARNING,
            )

    @admin.action(description=_("Archive (Soft Delete)"))
    def action_archive(self, request, queryset):
        ok = 0
        note = request.POST.get("review_note", "")

        with reversion.create_revision():
            for obj in queryset:
                if not self._user_has_perm(request, "archive", obj):
                    self.message_user(
                        request,
                        f"{obj}: " + _("You are not authorized to perform this action."),
                        level=messages.ERROR,
                    )
                    continue

                try:
                    try:
                        apply_editorial_action(
                            obj, EditorialAction.ARCHIVE, actor=request.user, note=note
                        )
                    except EditorialActionError as exc:
                        if exc.code in _NOT_EXECUTABLE_CODES:
                            raise RuntimeError(
                                _("Transition 'archive' not executable")
                            ) from exc
                        raise
                    ok += 1
                except Exception as e:  # noqa: BLE001 - one bad row must not abort the whole bulk action
                    self.message_user(
                        request, f"{obj}: {e}", level=messages.ERROR
                    )

            reversion.set_user(request.user)
            reversion.set_comment("archive")

        self.message_user(
            request,
            _("%(n)d item(s) archived.") % {"n": ok},
            level=messages.SUCCESS,
        )

    @admin.action(description=_("Restore (→ Draft)"))
    def action_restore_draft(self, request, queryset):
        ok = 0
        note = request.POST.get("review_note", "")

        with reversion.create_revision():
            for obj in queryset:
                if not self._user_has_perm(request, "restore", obj):
                    self.message_user(
                        request,
                        f"{obj}: " + _("You are not authorized to perform this action."),
                        level=messages.ERROR,
                    )
                    continue

                try:
                    try:
                        apply_editorial_action(
                            obj,
                            EditorialAction.RESTORE_TO_DRAFT,
                            actor=request.user,
                            note=note,
                        )
                    except EditorialActionError as exc:
                        if exc.code in _NOT_EXECUTABLE_CODES:
                            raise RuntimeError(
                                _("Transition 'restore' not executable")
                            ) from exc
                        raise
                    ok += 1
                except Exception as e:  # noqa: BLE001 - one bad row must not abort the whole bulk action
                    self.message_user(
                        request, f"{obj}: {e}", level=messages.ERROR
                    )

            reversion.set_user(request.user)
            reversion.set_comment("restore")

        self.message_user(
            request,
            _("%(n)d item(s) restored (draft).") % {"n": ok},
            level=messages.SUCCESS,
        )


class ChildOfGuideOwnershipMixin(admin.ModelAdmin):
    AUTHOR_GROUP_NAMES = getattr(settings, "AUTHOR_GROUP_NAMES", ["Author"])
    EDITOR_GROUP_NAMES = getattr(settings, "EDITOR_GROUP_NAMES", ["Editor", "Admin"])

    def _is_editor_user(self, user):
        if not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        return user.groups.filter(name__in=self.EDITOR_GROUP_NAMES).exists()

    def _is_author_of_guide(self, user, guide):
        if not user.is_authenticated or guide is None:
            return False
        return getattr(guide, "author_id", None) == user.id

    def _get_parent_guide(self, obj):
        """
        The owning ``Guide`` of whatever object a permission check was handed.

        A ``GuideSection`` carries ``guide`` directly. A
        ``GuideSectionTranslation`` does not: parler's
        ``TranslatableAdmin.delete_translation()`` passes the *translation*
        row to ``has_delete_permission()``, and that row reaches its guide only
        through ``master`` (the section). Resolving just the direct attribute
        therefore returned ``None`` for every per-language delete and refused
        it for every role - including the Admin group and superusers, who are
        entitled to it.

        Exactly these two shapes are resolved; anything else (``None``, an
        unsaved row whose FK raises, an unrelated object) stays ``None``, and
        every caller treats ``None`` as "deny".
        """
        guide = getattr(obj, "guide", None)
        if guide is not None:
            return guide

        master = getattr(obj, "master", None)
        if master is not None:
            return getattr(master, "guide", None)

        return None

    def has_change_permission(self, request, obj=None):
        base = super().has_change_permission(request, obj)
        if not base:
            return False

        if obj is None:
            return True

        guide = self._get_parent_guide(obj)
        if guide is None:
            return False

        if self._is_editor_user(request.user):
            return True

        return self._is_author_of_guide(request.user, guide)

    def has_delete_permission(self, request, obj=None):
        base = super().has_delete_permission(request, obj)
        if not base:
            return False

        if obj is None:
            return base

        guide = self._get_parent_guide(obj)
        if guide is None:
            return False

        if self._is_editor_user(request.user):
            return True

        return self._is_author_of_guide(request.user, guide)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if self._is_editor_user(request.user):
            return qs
        return qs.filter(guide__author=request.user)
