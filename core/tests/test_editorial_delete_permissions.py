"""
Beta 11.11D3B: the role-bound deletion contract for editorial content -
root single-delete and root bulk-delete.

The product rule this module locks in is *not* "nobody may ever hard-delete".
It is role-bound:

* **Author** and **Editor** own the state machine. They may edit, submit,
  review, archive and restore - and they may never physically delete, neither
  through the admin UI, nor through a direct delete URL, nor through a forged
  ``delete_selected`` POST.
* **Admin group** and **superuser** keep Django's ordinary hard-delete
  functions unchanged. A hard delete is never silently converted into an
  archive.

The mechanism that already produces this split is Django's own model-level
permission, seeded in ``accounts/signals.py::ensure_editorial_groups``: the
Author group gets ``add``/``view``, the Editor group ``add``/``change``/
``view``, and neither gets any ``delete_*`` codename. Both
``EditorialWorkflowAdminMixin.has_delete_permission`` and
``ChildOfGuideOwnershipMixin.has_delete_permission`` gate on
``super().has_delete_permission(...)`` *first*, so a missing model permission
is decisive before any object-level ownership branch is reached. The Admin
group is seeded with ``Permission.objects.all()`` and therefore keeps every
``delete_*``; superusers pass every permission check by definition.

These tests deliberately drive the real admin URLs through the test client
rather than calling ``has_delete_permission()`` directly - the point under
test is the reachable surface, not the predicate.
"""
import itertools

from django.contrib.admin.models import DELETION, LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import translation

from catalog.models import Tool
from compare.models import Comparison, ComparisonToolEntry
from core.models.editorial import EditorialWorkflowMixin as Workflow
from guides.models import Guide, GuideItem, GuideSection
from mentoroai.tests.utils import silence_django_request_warnings
from prompts.models import Prompt
from usecases.models import UseCase

User = get_user_model()

_counter = itertools.count()


def _unique(prefix):
    return f"{prefix}-{next(_counter)}"


def make_staff_user(username, *, group=None, superuser=False):
    """Mirrors ``core/tests/test_editorial_admin_permissions.py``'s helper -
    every role is a staff user so ``admin_site.admin_view()``'s staff gate is
    never what a test is accidentally measuring."""
    if superuser:
        user = User.objects.create_superuser(
            username=username, email=f"{username}@example.com", password="pw"
        )
    else:
        user = User.objects.create_user(username=username, password="pw", is_staff=True)
    if group:
        user.groups.add(Group.objects.get(name=group))
    return user


# ======================================================================
# Object factories - one per admin surface under test
# ======================================================================


def make_prompt(*, author, languages=("en",)):
    with translation.override("en"):
        obj = Prompt.objects.create(author=author, status=Workflow.STATUS_DRAFT)
        for code in languages:
            obj.create_translation(
                code,
                title=f"Prompt {code}",
                intro="i",
                body="b",
                outro="o",
                slug=_unique(f"d3b-prompt-{code}"),
            )
    return obj


def make_guide(*, author, languages=("en",)):
    with translation.override("en"):
        obj = Guide.objects.create(author=author, status=Workflow.STATUS_DRAFT)
        for code in languages:
            obj.create_translation(
                code,
                title=f"Guide {code}",
                intro="i",
                body="b",
                slug=_unique(f"d3b-guide-{code}"),
            )
    return obj


def make_usecase(*, author, languages=("en",)):
    with translation.override("en"):
        obj = UseCase.objects.create(author=author, status=Workflow.STATUS_DRAFT)
        for code in languages:
            obj.create_translation(
                code,
                title=f"UseCase {code}",
                intro="i",
                body="b",
                outro="o",
                persona="p",
                slug=_unique(f"d3b-usecase-{code}"),
            )
    return obj


def make_comparison(*, author, languages=("en",)):
    with translation.override("en"):
        obj = Comparison.objects.create(author=author, status=Workflow.STATUS_DRAFT)
        for code in languages:
            obj.create_translation(
                code,
                title=f"Comparison {code}",
                intro="i",
                body="b",
                slug=_unique(f"d3b-comparison-{code}"),
            )
    return obj


def make_guide_section(*, author, languages=("en",)):
    guide = make_guide(author=author, languages=("en",))
    with translation.override("en"):
        section = GuideSection.objects.create(guide=guide, order=0)
        for code in languages:
            section.create_translation(code, title=f"Section {code}", body="sb")
    return section


#: The four editorial roots. ``EditorialWorkflowAdminMixin.has_change_permission``
#: grants an Author the change form for their own object, so both Author and
#: Editor can open these.
ROOT_ADMIN_SURFACES = (
    ("prompt", "prompts_prompt", Prompt, make_prompt),
    ("guide", "guides_guide", Guide, make_guide),
    ("usecase", "usecases_usecase", UseCase, make_usecase),
    ("comparison", "compare_comparison", Comparison, make_comparison),
)

#: ``GuideSection`` has its own registered admin. Unlike the roots,
#: ``ChildOfGuideOwnershipMixin.has_change_permission`` gates on Django's
#: ``guides.change_guidesection`` first, which the Author group does not hold -
#: so an Author cannot open this change form at all (asserted separately in
#: :class:`GuideSectionAdminIsUnreachableForAuthorsTests`). Editor holds
#: ``change_guidesection`` and can.
CHILD_ADMIN_SURFACES = (
    ("guidesection", "guides_guidesection", GuideSection, make_guide_section),
)

#: Every surface, for the URL-level checks where the answer must be identical
#: for every non-privileged role regardless of change-form reachability.
ALL_ADMIN_SURFACES = ROOT_ADMIN_SURFACES + CHILD_ADMIN_SURFACES


def delete_url(url_prefix, pk):
    return reverse(f"admin:{url_prefix}_delete", args=[pk])


def change_url(url_prefix, pk):
    return reverse(f"admin:{url_prefix}_change", args=[pk])


def changelist_url(url_prefix):
    return reverse(f"admin:{url_prefix}_changelist")


def deletion_log_entries():
    return LogEntry.objects.filter(action_flag=DELETION)


def model_admin_for(model):
    from django.contrib import admin as django_admin

    return django_admin.site._registry[model]


def request_for(user):
    request = RequestFactory().get("/")
    request.user = user
    return request


# ======================================================================
# 6.1 Root single delete - Author and Editor are denied
# ======================================================================


class RootSingleDeleteDeniedForAuthorAndEditorTests(TestCase):
    """Author (own *and* foreign object) and Editor must have no delete
    button, and both the GET and the POST of the direct delete URL must be
    refused with the row left completely intact."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = make_staff_user("d3b-owner", group="Author")
        cls.other_author = make_staff_user("d3b-other-author", group="Author")
        cls.editor = make_staff_user("d3b-editor", group="Editor")

    def test_root_change_form_offers_no_delete_button(self):
        for label, url_prefix, _model, factory in ROOT_ADMIN_SURFACES:
            obj = factory(author=self.owner)
            for role, user in (("author-owner", self.owner), ("editor", self.editor)):
                with self.subTest(surface=label, role=role):
                    self.client.force_login(user)
                    resp = self.client.get(change_url(url_prefix, obj.pk))
                    self.assertEqual(resp.status_code, 200)
                    self.assertFalse(resp.context["has_delete_permission"])
                    self.assertNotContains(resp, delete_url(url_prefix, obj.pk))

    def test_guide_section_change_form_offers_no_delete_button_to_an_editor(self):
        section = make_guide_section(author=self.owner)
        self.client.force_login(self.editor)
        resp = self.client.get(change_url("guides_guidesection", section.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context["has_delete_permission"])
        self.assertNotContains(resp, delete_url("guides_guidesection", section.pk))

    def test_direct_delete_get_is_refused(self):
        for label, url_prefix, model, factory in ALL_ADMIN_SURFACES:
            obj = factory(author=self.owner)
            for role, user in (
                ("author-owner", self.owner),
                ("author-foreign", self.other_author),
                ("editor", self.editor),
            ):
                with self.subTest(surface=label, role=role):
                    self.client.force_login(user)
                    with silence_django_request_warnings():
                        resp = self.client.get(delete_url(url_prefix, obj.pk))
                    self.assertEqual(resp.status_code, 403)
                    self.assertTrue(model.objects.filter(pk=obj.pk).exists())

    def test_direct_delete_post_is_refused_and_nothing_is_removed(self):
        for label, url_prefix, model, factory in ALL_ADMIN_SURFACES:
            obj = factory(author=self.owner)
            for role, user in (
                ("author-owner", self.owner),
                ("author-foreign", self.other_author),
                ("editor", self.editor),
            ):
                with self.subTest(surface=label, role=role):
                    self.client.force_login(user)
                    with silence_django_request_warnings():
                        resp = self.client.post(
                            delete_url(url_prefix, obj.pk), data={"post": "yes"}
                        )
                    self.assertEqual(resp.status_code, 403)
                    self.assertTrue(model.objects.filter(pk=obj.pk).exists())

    def test_a_refused_delete_writes_no_deletion_log_entry(self):
        for label, url_prefix, _model, factory in ALL_ADMIN_SURFACES:
            obj = factory(author=self.owner)
            with self.subTest(surface=label):
                self.client.force_login(self.owner)
                with silence_django_request_warnings():
                    self.client.post(delete_url(url_prefix, obj.pk), data={"post": "yes"})
        self.assertFalse(deletion_log_entries().exists())

    def test_translations_and_children_survive_a_refused_delete(self):
        guide = make_guide(author=self.owner, languages=("en", "de"))
        section = GuideSection.objects.create(guide=guide, order=0)
        section.create_translation("en", title="S", body="b")
        item = GuideItem.objects.create(section=section, order=0, url="https://example.com/a")
        item.create_translation("en", title="I", teaser="t")

        self.client.force_login(self.owner)
        with silence_django_request_warnings():
            self.client.post(delete_url("guides_guide", guide.pk), data={"post": "yes"})

        self.assertTrue(Guide.objects.filter(pk=guide.pk).exists())
        self.assertEqual(
            sorted(Guide.objects.get(pk=guide.pk).get_available_languages()), ["de", "en"]
        )
        self.assertTrue(GuideSection.objects.filter(pk=section.pk).exists())
        self.assertTrue(GuideItem.objects.filter(pk=item.pk).exists())


class GuideSectionAdminIsReadOnlyForAuthorsTests(TestCase):
    """Pre-existing, unchanged contract worth pinning next to the delete
    matrix.

    The Author group holds ``guides.view_guidesection`` but not
    ``guides.change_guidesection``, so Django's
    ``ModelAdmin._changeform_view`` admits the GET through
    ``has_view_or_change_permission()`` and renders a **read-only** form
    (status 200), while every mutating path stays closed: no delete button,
    delete URL refused, and - per ``InlineModelAdmin.get_formset``'s
    ``DeleteProtectedModelForm.has_changed()`` - inline edits silently
    discarded."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = make_staff_user("gs-owner", group="Author")
        cls.section = make_guide_section(author=cls.owner)

    def test_author_gets_a_read_only_change_form(self):
        self.client.force_login(self.owner)
        resp = self.client.get(change_url("guides_guidesection", self.section.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context["has_change_permission"])
        self.assertFalse(resp.context["has_delete_permission"])
        self.assertNotContains(resp, delete_url("guides_guidesection", self.section.pk))

    def test_author_cannot_delete_the_guide_section(self):
        self.client.force_login(self.owner)
        with silence_django_request_warnings():
            resp = self.client.post(
                delete_url("guides_guidesection", self.section.pk), data={"post": "yes"}
            )
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(GuideSection.objects.filter(pk=self.section.pk).exists())


# ======================================================================
# 6.1 Root single delete - Admin group and superuser keep Django's default
# ======================================================================


class RootSingleDeleteAllowedForAdminAndSuperuserTests(TestCase):
    """The Admin group and superusers keep the ordinary Django hard delete -
    button, confirmation page, real removal, and the ordinary cascades."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = make_staff_user("d3b-del-owner", group="Author")
        cls.admin_group = make_staff_user("d3b-admin-group", group="Admin")
        cls.superuser = make_staff_user("d3b-superuser", superuser=True)

    def test_change_form_offers_the_delete_button(self):
        for role, user in (("admin-group", self.admin_group), ("superuser", self.superuser)):
            for label, url_prefix, _model, factory in ALL_ADMIN_SURFACES:
                obj = factory(author=self.owner)
                with self.subTest(surface=label, role=role):
                    self.client.force_login(user)
                    resp = self.client.get(change_url(url_prefix, obj.pk))
                    self.assertEqual(resp.status_code, 200)
                    self.assertTrue(resp.context["has_delete_permission"])
                    self.assertContains(resp, delete_url(url_prefix, obj.pk))

    def test_direct_delete_get_renders_the_normal_confirmation(self):
        for role, user in (("admin-group", self.admin_group), ("superuser", self.superuser)):
            for label, url_prefix, _model, factory in ALL_ADMIN_SURFACES:
                obj = factory(author=self.owner)
                with self.subTest(surface=label, role=role):
                    self.client.force_login(user)
                    resp = self.client.get(delete_url(url_prefix, obj.pk))
                    self.assertEqual(resp.status_code, 200)
                    self.assertIn("deleted_objects", resp.context)

    def test_delete_post_physically_removes_the_row(self):
        for role, user in (("admin-group", self.admin_group), ("superuser", self.superuser)):
            for label, url_prefix, model, factory in ALL_ADMIN_SURFACES:
                obj = factory(author=self.owner)
                with self.subTest(surface=label, role=role):
                    self.client.force_login(user)
                    resp = self.client.post(
                        delete_url(url_prefix, obj.pk), data={"post": "yes"}
                    )
                    self.assertEqual(resp.status_code, 302)
                    self.assertFalse(model.objects.filter(pk=obj.pk).exists())

    def test_hard_delete_is_never_converted_into_an_archive(self):
        guide = make_guide(author=self.owner)
        self.client.force_login(self.admin_group)
        self.client.post(delete_url("guides_guide", guide.pk), data={"post": "yes"})
        self.assertFalse(Guide.objects.filter(pk=guide.pk).exists())

    def test_ordinary_django_cascades_still_follow_the_root(self):
        guide = make_guide(author=self.owner)
        section = GuideSection.objects.create(guide=guide, order=0)
        section.create_translation("en", title="S", body="b")
        item = GuideItem.objects.create(section=section, order=0, url="https://example.com/a")
        item.create_translation("en", title="I", teaser="t")

        self.client.force_login(self.superuser)
        self.client.post(delete_url("guides_guide", guide.pk), data={"post": "yes"})

        self.assertFalse(Guide.objects.filter(pk=guide.pk).exists())
        self.assertFalse(GuideSection.objects.filter(pk=section.pk).exists())
        self.assertFalse(GuideItem.objects.filter(pk=item.pk).exists())


# ======================================================================
# 6.2 Bulk delete
# ======================================================================


class BulkDeleteDeniedForAuthorAndEditorTests(TestCase):
    """``delete_selected`` is a site-wide action carrying
    ``allowed_permissions=["delete"]``. ``ModelAdmin._filter_actions_by_permissions``
    drops it for any user whose ``has_delete_permission(request)`` (obj=None)
    is False, which removes it from ``get_actions()``, from the rendered
    action dropdown, *and* - because ``response_action`` validates the posted
    action name against ``get_action_choices()`` - from a forged POST."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = make_staff_user("d3b-bulk-owner", group="Author")
        cls.editor = make_staff_user("d3b-bulk-editor", group="Editor")

    def test_delete_selected_is_absent_from_get_actions(self):
        for role, user in (("author", self.owner), ("editor", self.editor)):
            for label, _url_prefix, model, _factory in ALL_ADMIN_SURFACES:
                with self.subTest(surface=label, role=role):
                    actions = model_admin_for(model).get_actions(request_for(user))
                    self.assertNotIn("delete_selected", actions)

    def test_delete_selected_is_absent_from_the_rendered_changelist(self):
        for role, user in (("author", self.owner), ("editor", self.editor)):
            for label, url_prefix, _model, factory in ALL_ADMIN_SURFACES:
                factory(author=self.owner)
                with self.subTest(surface=label, role=role):
                    self.client.force_login(user)
                    resp = self.client.get(changelist_url(url_prefix))
                    self.assertEqual(resp.status_code, 200)
                    action_form = resp.context["action_form"]
                    if action_form is None:
                        # ``GuideSectionAdmin`` declares no actions of its own,
                        # so once ``delete_selected`` is filtered out there is
                        # nothing left and Django renders no action bar at all.
                        continue
                    choices = dict(action_form.fields["action"].choices)
                    self.assertNotIn("delete_selected", choices)

    def test_forged_bulk_delete_post_deletes_nothing(self):
        for role, user in (("author", self.owner), ("editor", self.editor)):
            for label, url_prefix, model, factory in ALL_ADMIN_SURFACES:
                kept = factory(author=self.owner)
                also_kept = factory(author=self.owner)
                with self.subTest(surface=label, role=role):
                    self.client.force_login(user)
                    self.client.post(
                        changelist_url(url_prefix),
                        data={
                            "action": "delete_selected",
                            "_selected_action": [str(kept.pk)],
                            "index": "0",
                            "post": "yes",
                        },
                        follow=True,
                    )
                    self.assertTrue(model.objects.filter(pk=kept.pk).exists())
                    self.assertTrue(model.objects.filter(pk=also_kept.pk).exists())

    def test_forged_bulk_delete_across_a_whole_selection_deletes_nothing(self):
        guides = [make_guide(author=self.owner) for _ in range(3)]
        self.client.force_login(self.editor)
        self.client.post(
            changelist_url("guides_guide"),
            data={
                "action": "delete_selected",
                "_selected_action": [str(g.pk) for g in guides],
                "index": "0",
                "post": "yes",
            },
            follow=True,
        )
        for guide in guides:
            self.assertTrue(Guide.objects.filter(pk=guide.pk).exists())

    def test_forged_bulk_delete_writes_no_deletion_log_entry(self):
        guide = make_guide(author=self.owner)
        self.client.force_login(self.owner)
        self.client.post(
            changelist_url("guides_guide"),
            data={
                "action": "delete_selected",
                "_selected_action": [str(guide.pk)],
                "index": "0",
                "post": "yes",
            },
            follow=True,
        )
        self.assertFalse(deletion_log_entries().exists())

    def test_workflow_actions_are_still_offered(self):
        """Removing nothing but ``delete_selected``: the six editorial
        workflow actions must stay available to both roles."""
        for role, user in (("author", self.owner), ("editor", self.editor)):
            with self.subTest(role=role):
                actions = model_admin_for(Guide).get_actions(request_for(user))
                self.assertIn("action_archive", actions)
                self.assertIn("action_restore_draft", actions)
                self.assertIn("action_submit_for_review", actions)


class BulkDeleteAllowedForAdminAndSuperuserTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = make_staff_user("d3b-bulk-a-owner", group="Author")
        cls.admin_group = make_staff_user("d3b-bulk-admin", group="Admin")
        cls.superuser = make_staff_user("d3b-bulk-su", superuser=True)

    def test_delete_selected_is_present_in_get_actions(self):
        for role, user in (("admin-group", self.admin_group), ("superuser", self.superuser)):
            for label, _url_prefix, model, _factory in ALL_ADMIN_SURFACES:
                with self.subTest(surface=label, role=role):
                    actions = model_admin_for(model).get_actions(request_for(user))
                    self.assertIn("delete_selected", actions)

    def test_confirmation_page_is_reachable(self):
        guide = make_guide(author=self.owner)
        self.client.force_login(self.admin_group)
        resp = self.client.post(
            changelist_url("guides_guide"),
            data={
                "action": "delete_selected",
                "_selected_action": [str(guide.pk)],
                "index": "0",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("deletable_objects", resp.context)
        self.assertTrue(Guide.objects.filter(pk=guide.pk).exists())

    def test_confirmed_bulk_delete_removes_only_the_selection(self):
        for role, user in (("admin-group", self.admin_group), ("superuser", self.superuser)):
            for label, url_prefix, model, factory in ALL_ADMIN_SURFACES:
                doomed = factory(author=self.owner)
                survivor = factory(author=self.owner)
                with self.subTest(surface=label, role=role):
                    self.client.force_login(user)
                    self.client.post(
                        changelist_url(url_prefix),
                        data={
                            "action": "delete_selected",
                            "_selected_action": [str(doomed.pk)],
                            "index": "0",
                            "post": "yes",
                        },
                        follow=True,
                    )
                    self.assertFalse(model.objects.filter(pk=doomed.pk).exists())
                    self.assertTrue(model.objects.filter(pk=survivor.pk).exists())


# ======================================================================
# 6.5 Archive and restore stay available for Author and Editor
# ======================================================================


class ArchiveAndRestoreRemainAvailableTests(TestCase):
    """The state machine is what Author and Editor use instead of deleting.
    Driven through the real production surfaces: the editorial view for the
    author, ``core.admin``'s changelist actions for the editor."""

    @classmethod
    def setUpTestData(cls):
        cls.author = make_staff_user("d3b-ar-author", group="Author")
        cls.editor = make_staff_user("d3b-ar-editor", group="Editor")

    def _post_status(self, user, model_key, obj, status):
        self.client.force_login(user)
        return self.client.post(
            reverse("content:editorial:my_content_update"),
            data={"model": model_key, "object_id": str(obj.pk), "status": status},
            follow=True,
        )

    def _publish_guide_through_the_real_workflow(self):
        guide = make_guide(author=self.author)
        self._post_status(self.author, "guide", guide, "review")
        self._post_status(self.editor, "guide", guide, "approved")
        self._post_status(self.author, "guide", guide, "published")
        guide = Guide.objects.get(pk=guide.pk)
        self.assertEqual(guide.status, Workflow.STATUS_PUBLISHED)
        self.assertTrue(guide.is_published)
        self.assertTrue(guide.live_i18n)
        return guide

    def _visible_pks(self):
        return list(Guide.objects.visible_in_language("en").values_list("pk", flat=True))

    def test_author_can_archive_and_restore_a_published_guide(self):
        guide = self._publish_guide_through_the_real_workflow()
        snapshot_before = dict(guide.live_i18n)
        self.assertIn(guide.pk, self._visible_pks())

        self._post_status(self.author, "guide", guide, "archived")
        archived = Guide.objects.get(pk=guide.pk)
        self.assertEqual(archived.status, Workflow.STATUS_ARCHIVED)
        self.assertFalse(archived.is_published)
        self.assertEqual(archived.live_i18n, snapshot_before)
        self.assertNotIn(archived.pk, self._visible_pks())

        self._post_status(self.author, "guide", guide, "draft")
        restored = Guide.objects.get(pk=guide.pk)
        self.assertEqual(restored.status, Workflow.STATUS_DRAFT)
        self.assertFalse(restored.is_published)
        self.assertEqual(restored.live_i18n, snapshot_before)
        self.assertNotIn(restored.pk, self._visible_pks())

    def test_editor_can_archive_and_restore_through_the_admin_actions(self):
        guide = self._publish_guide_through_the_real_workflow()
        self.client.force_login(self.editor)

        self.client.post(
            changelist_url("guides_guide"),
            data={
                "action": "action_archive",
                "_selected_action": [str(guide.pk)],
                "index": "0",
            },
            follow=True,
        )
        archived = Guide.objects.get(pk=guide.pk)
        self.assertEqual(archived.status, Workflow.STATUS_ARCHIVED)
        self.assertFalse(archived.is_published)

        self.client.post(
            changelist_url("guides_guide"),
            data={
                "action": "action_restore_draft",
                "_selected_action": [str(guide.pk)],
                "index": "0",
            },
            follow=True,
        )
        restored = Guide.objects.get(pk=guide.pk)
        self.assertEqual(restored.status, Workflow.STATUS_DRAFT)
        self.assertFalse(restored.is_published)

    def test_archiving_is_available_while_hard_delete_is_not(self):
        guide = make_guide(author=self.author)
        self._post_status(self.author, "guide", guide, "archived")
        self.assertEqual(Guide.objects.get(pk=guide.pk).status, Workflow.STATUS_ARCHIVED)

        self.client.force_login(self.author)
        with silence_django_request_warnings():
            resp = self.client.post(
                delete_url("guides_guide", guide.pk), data={"post": "yes"}
            )
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(Guide.objects.filter(pk=guide.pk).exists())

    def test_comparison_entries_survive_an_archive(self):
        tool = Tool.objects.create(slug=_unique("d3b-tool"))
        tool.create_translation("en", name="Tool")
        comparison = make_comparison(author=self.author)
        entry = ComparisonToolEntry.objects.create(
            comparison=comparison, tool=tool, position=0
        )

        self._post_status(self.author, "comparison", comparison, "archived")

        self.assertEqual(
            Comparison.objects.get(pk=comparison.pk).status, Workflow.STATUS_ARCHIVED
        )
        self.assertTrue(ComparisonToolEntry.objects.filter(pk=entry.pk).exists())
