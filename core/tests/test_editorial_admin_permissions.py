"""
Beta 11.1: verifies and locks in the fix for
EditorialWorkflowAdminMixin.has_change_permission(request, obj=None)
unconditionally returning True.

Confirmed vulnerability (pre-fix): any authenticated is_staff=True user -
even one in no editorial group and holding zero Django model permissions -
could open any of Guide/Prompt/UseCase/Comparison's admin changelist by
navigating directly to its URL, because has_change_permission(obj=None)
short-circuited to True before ever consulting Django's real permission
system or group membership.

Django's own has_module_permission() (which governs whether the app
appears on the admin index page at all) is unrelated to this override and
was already correctly restricted by the real per-app Django permissions the
"Admin"/"Editor"/"Author" groups are seeded with (see
accounts/signals.py::ensure_editorial_groups) - so the module never
appeared on the admin index dashboard for an unprivileged staff user, but
the changelist URL itself was reachable regardless of that. This module
only covers the confirmed changelist-URL leak.

The fix keeps the intended Author/Editor/Admin contract completely intact:
Author only has Django's add_/view_ permissions, never change_, and relies
entirely on the object-level branch of this same override for self-editing
(see accounts/tests/test_groups.py) - untouched by this fix.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from core.models.editorial import EditorialWorkflowMixin
from guides.models import Guide

User = get_user_model()

CONTENT_TYPE_URL_PREFIXES = [
    "guides_guide",
    "prompts_prompt",
    "usecases_usecase",
    "compare_comparison",
]


def make_staff_user(username, *, group=None, superuser=False):
    if superuser:
        user = User.objects.create_superuser(
            username=username, email=f"{username}@example.com", password="pw"
        )
    else:
        user = User.objects.create_user(username=username, password="pw", is_staff=True)
    if group:
        user.groups.add(Group.objects.get(name=group))
    return user


class ChangelistAccessWithoutRoleTests(TestCase):
    """Plain is_staff, no editorial group, no Django model permission ->
    changelist must be denied for every editorial content type."""

    def setUp(self):
        self.bystander_staff = make_staff_user("bystander-staff")

    def test_changelist_denied_for_every_content_type(self):
        self.client.force_login(self.bystander_staff)
        for url_prefix in CONTENT_TYPE_URL_PREFIXES:
            with self.subTest(model=url_prefix):
                url = reverse(f"admin:{url_prefix}_changelist")
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 403)

    def test_change_form_of_someone_elses_object_is_also_denied(self):
        author = make_staff_user("bystander-owner", group="Author")
        guide = Guide.objects.create(author=author, status=EditorialWorkflowMixin.STATUS_DRAFT)
        guide.create_translation("en", title="G", slug="bystander-target-guide", intro="i", body="b")

        self.client.force_login(self.bystander_staff)
        resp = self.client.get(reverse("admin:guides_guide_change", args=[guide.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_non_staff_user_is_redirected_not_denied(self):
        # Distinguishes "denied" (403: is_staff but no editorial role) from
        # "not let into /admin/ at all" (302: Django's own staff gate).
        non_staff = User.objects.create_user(username="non-staff", password="pw", is_staff=False)
        self.client.force_login(non_staff)
        resp = self.client.get(reverse("admin:guides_guide_changelist"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin/login/", resp.url)


class ChangelistAccessWithRoleTests(TestCase):
    """Author/Editor/Admin/superuser all keep changelist access - the fix
    must not narrow their existing, intended contract."""

    def test_author_editor_admin_and_superuser_all_reach_every_changelist(self):
        roles = {
            "author": make_staff_user("role-author", group="Author"),
            "editor": make_staff_user("role-editor", group="Editor"),
            "admin-group": make_staff_user("role-admin-group", group="Admin"),
            "superuser": make_staff_user("role-superuser", superuser=True),
        }
        for role_name, user in roles.items():
            self.client.force_login(user)
            for url_prefix in CONTENT_TYPE_URL_PREFIXES:
                with self.subTest(role=role_name, model=url_prefix):
                    resp = self.client.get(reverse(f"admin:{url_prefix}_changelist"))
                    self.assertEqual(resp.status_code, 200)
            self.client.logout()


class ObjectLevelChangePermissionTests(TestCase):
    """Guide is representative: EditorialWorkflowAdminMixin's object-level
    branch (obj is not None) is shared, untouched code - Author can edit
    only their own content, Editor/Admin can edit anything, and a
    non-owning Author gets a view-only form rather than a 403."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = make_staff_user("perm-owner", group="Author")
        cls.other_author = make_staff_user("perm-other-author", group="Author")
        cls.editor = make_staff_user("perm-editor", group="Editor")

        cls.guide = Guide.objects.create(author=cls.owner, status=EditorialWorkflowMixin.STATUS_DRAFT)
        cls.guide.create_translation("en", title="Owned Guide", slug="owned-guide-perm", intro="i", body="b")

    def _change_url(self):
        return reverse("admin:guides_guide_change", args=[self.guide.pk])

    def test_owning_author_gets_editable_form(self):
        self.client.force_login(self.owner)
        resp = self.client.get(self._change_url())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("intro", resp.context["adminform"].form.fields)

    def test_non_owning_author_gets_view_only_form_not_denied(self):
        self.client.force_login(self.other_author)
        resp = self.client.get(self._change_url())
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("intro", resp.context["adminform"].form.fields)

    def test_editor_can_edit_content_that_is_not_their_own(self):
        self.client.force_login(self.editor)
        resp = self.client.get(self._change_url())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("intro", resp.context["adminform"].form.fields)


class WorkflowActionServerSideEnforcementTests(TestCase):
    """Actions stay listed for every role that can reach the changelist
    (Django's default get_actions() behavior, unchanged in this slice -
    see final report), but self-approval is still blocked server-side by
    core/authz.py's rules regardless of what the UI offers."""

    @classmethod
    def setUpTestData(cls):
        cls.author = make_staff_user("wf-author", group="Author")
        cls.guide = Guide.objects.create(author=cls.author, status=EditorialWorkflowMixin.STATUS_REVIEW)
        cls.guide.create_translation("en", title="Review Guide", slug="wf-review-guide", intro="i", body="b")

    def test_author_cannot_approve_their_own_content_via_admin_action(self):
        self.client.force_login(self.author)
        url = reverse("admin:guides_guide_changelist")
        resp = self.client.post(
            url,
            data={"action": "action_approve", "_selected_action": [str(self.guide.pk)]},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            Guide.objects.get(pk=self.guide.pk).status, EditorialWorkflowMixin.STATUS_REVIEW
        )
