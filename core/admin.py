import reversion
from django.conf import settings
from django.contrib import admin, messages
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from django_fsm import can_proceed
from parler.admin import TranslatableAdmin
from reversion.models import Version
from tinymce.widgets import TinyMCE


def set_last_published_revision(obj):
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
        css = {
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


class TranslatableTinyMCEInlineMixin:
    """
    Inline admin counterpart that applies TinyMCE to translation fields inside inlines
    to keep the editing experience consistent.
    """
    tinymce_fields: tuple[str, ...] = ()
    wide_text_inputs: tuple[str, ...] = ("title", "label")

    class Media:
        css = {
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

    workflow_actions = [
        "action_submit_for_review",
        "action_request_rework",
        "action_approve",
        "action_publish",
        "action_archive",
        "action_restore_draft",
    ]

    # Standard description for auto review note; can be overwritten per admin class
    auto_review_note = _("Auto: Change in the admin form")

    actions = workflow_actions

    # ---- internal helper ----

    def _get_transition(self, obj, name: str):
        return getattr(obj, name, None)

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
            if getattr(fs, "changed_objects", None):
                if fs.changed_objects:
                    return True
            if getattr(fs, "new_objects", None):
                if fs.new_objects:
                    return True
            if getattr(fs, "deleted_objects", None):
                if fs.deleted_objects:
                    return True

            # Formset-API
            if getattr(fs, "changed_forms", None):
                if fs.changed_forms:
                    return True
            if getattr(fs, "new_forms", None):
                if fs.new_forms:
                    return True
            if getattr(fs, "deleted_forms", None):
                if fs.deleted_forms:
                    return True

        return False

    def _auto_transition_to_review(self, request, obj):
        """
        Automatically moves an already published object to REVIEW,
        If the user is allowed and the transition is available.
        """
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
            return True

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
        if not self._is_editor_user(request.user):
            if "author" in [f.name for f in self.model._meta.fields]:
                fields.append("author")

        if "published_at" in [f.name for f in self.model._meta.fields]:
            if not self._is_admin_user(request.user):
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
        """
        obj = form.instance
        inline_changed = self._inlines_changed(formsets)

        if inline_changed and getattr(obj, "status", None) == getattr(
                obj, "STATUS_PUBLISHED", "published"
        ):
            if self._user_has_perm(request, "submit_for_review", obj):
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

        with transaction.atomic():
            with reversion.create_revision():
                reversion.set_user(request.user)
                reversion.set_comment("submit_for_review")

                for obj in queryset.select_for_update():
                    if not self._user_has_perm(request, "submit_for_review", obj):
                        skipped.append((obj.pk, _("permission denied")))
                        continue

                    transition = self._get_transition(obj, "move_to_review")
                    if transition and can_proceed(transition):
                        try:
                            transition(
                                by=request.user,
                                note="Admin-Action: submit_for_review",
                            )
                        except TypeError:
                            transition()
                        self._save_with_fields(
                            obj, "status", "submitted_for_review_at", "updated_at"
                        )
                        moved += 1
                    else:
                        skipped.append(
                            (obj.pk, _("Transition 'move_to_review' not executable"))
                        )

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

                    transition = self._get_transition(obj, "request_rework")
                    if not transition or not can_proceed(transition):
                        raise RuntimeError(_("Transition 'request_rework' not executable"))

                    transition(by=request.user, note=note)
                    self._save_with_fields(
                        obj,
                        "status",
                        "review_note",
                        "reviewed_at",
                        "reviewed_by",
                        "updated_at",
                    )
                    ok += 1
                except Exception as e:
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

        with transaction.atomic():
            with reversion.create_revision():
                reversion.set_user(request.user)
                reversion.set_comment("approve")

                note = request.POST.get("review_note", "")

                for obj in queryset.select_for_update():
                    if not self._user_has_perm(request, "approve", obj):
                        skipped.append((obj.pk, _("permission denied")))
                        continue

                    transition = self._get_transition(obj, "approve")
                    if not transition or not can_proceed(transition):
                        skipped.append((obj.pk, _("transition 'approve' not executable")))
                        continue

                    try:
                        transition(by=request.user, note=note)
                    except TypeError:
                        transition()

                    self._save_with_fields(
                        obj,
                        "status",
                        "review_note",
                        "reviewed_at",
                        "reviewed_by",
                        "updated_at",
                    )
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

        with transaction.atomic():
            with reversion.create_revision():
                reversion.set_user(request.user)
                reversion.set_comment("Admin-Action: publish")

                for obj in queryset.select_for_update():
                    if not self._user_has_perm(request, "publish", obj):
                        skipped.append((obj.pk, _("permission denied")))
                        continue

                    if getattr(obj, "status", None) == getattr(obj, "STATUS_PUBLISHED", "published"):
                        continue

                    transition = self._get_transition(obj, "publish")
                    if transition and can_proceed(transition):
                        try:
                            transition(by=request.user, note="Admin-Action publish")
                        except TypeError:
                            transition()

                        obj.save()
                        set_last_published_revision(obj)
                        self._save_with_fields(
                            obj, "last_published_revision_id"
                        )
                        published += 1
                    else:
                        skipped.append(
                            (obj.pk, _("Transition 'publish' not executable"))
                        )

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
                    transition = self._get_transition(obj, "archive")
                    if not transition or not can_proceed(transition):
                        raise RuntimeError(_("Transition 'archive' not executable"))

                    transition(by=request.user, note=note)
                    obj.is_published = False
                    self._save_with_fields(
                        obj, "status", "review_note", "is_published", "updated_at"
                    )
                    ok += 1
                except Exception as e:
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
                    transition = self._get_transition(obj, "restore")
                    if not transition or not can_proceed(transition):
                        raise RuntimeError(_("Transition 'restore' not executable"))

                    transition(by=request.user, note=note)
                    obj.is_published = False
                    self._save_with_fields(
                        obj, "status", "review_note", "is_published", "updated_at"
                    )
                    ok += 1
                except Exception as e:
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
        return getattr(obj, "guide", None)

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
