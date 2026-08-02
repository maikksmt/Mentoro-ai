"""
Beta 11.11D3B: the role-bound deletion contract for editorial content -
django-parler's per-language "delete translation" surface.

Parler's own contract (django-parler 2.3):

* the link is rendered by ``admin/parler/language_tabs.html`` and gated
  *only* by ``language_tabs.allow_deletion``, which
  ``parler.utils.views.get_language_tabs()`` sets to
  ``len(available_languages) > 1`` - **no permission is consulted there**;
* the view ``TranslatableAdmin.delete_translation()`` gates on
  ``self.has_delete_permission(request, translation)`` and raises
  ``PermissionDenied`` otherwise;
* a single remaining translation is refused via ``deletion_not_allowed()``;
* on a confirmed POST, ``delete_model_translation()`` removes the master's
  translation row *and* - because ``delete_inline_translations`` defaults to
  ``True`` - the same language's rows of every translatable inline.

So the server side already implements the role-bound rule (Author and Editor
hold no ``delete_*`` model permission, see
``accounts/signals.py::ensure_editorial_groups``), while the *UI* offered the
link to them regardless. This module pins both halves: the link must follow
the same permission the view enforces, and Admin/superuser must keep parler's
behaviour completely unchanged.
"""
import itertools

from django.contrib.admin.models import DELETION, LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import translation
from reversion.models import Revision

from catalog.models import Tool
from compare.models import (
    Comparison,
    ComparisonToolEntry,
    ComparisonToolEntryTranslation,
    ComparisonTranslation,
)
from core.models.editorial import EditorialWorkflowMixin as Workflow
from guides.models import Guide, GuideSection, GuideSectionTranslation, GuideTranslation
from mentoroai.tests.utils import silence_django_request_warnings
from prompts.models import Prompt, PromptTranslation
from usecases.models import UseCase, UseCaseTranslation

User = get_user_model()

_counter = itertools.count()


def _unique(prefix):
    return f"{prefix}-{next(_counter)}"


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


def make_prompt(*, author, languages=("en", "de")):
    with translation.override("en"):
        obj = Prompt.objects.create(author=author, status=Workflow.STATUS_DRAFT)
        for code in languages:
            obj.create_translation(
                code, title=f"Prompt {code}", intro="i", body="b", outro="o",
                slug=_unique(f"td-prompt-{code}"),
            )
    return obj


def make_guide(*, author, languages=("en", "de")):
    with translation.override("en"):
        obj = Guide.objects.create(author=author, status=Workflow.STATUS_DRAFT)
        for code in languages:
            obj.create_translation(
                code, title=f"Guide {code}", intro="i", body="b",
                slug=_unique(f"td-guide-{code}"),
            )
    return obj


def make_usecase(*, author, languages=("en", "de")):
    with translation.override("en"):
        obj = UseCase.objects.create(author=author, status=Workflow.STATUS_DRAFT)
        for code in languages:
            obj.create_translation(
                code, title=f"UseCase {code}", intro="i", body="b", outro="o",
                persona="p", slug=_unique(f"td-usecase-{code}"),
            )
    return obj


def make_comparison(*, author, languages=("en", "de")):
    with translation.override("en"):
        obj = Comparison.objects.create(author=author, status=Workflow.STATUS_DRAFT)
        for code in languages:
            obj.create_translation(
                code, title=f"Comparison {code}", intro="i", body="b",
                slug=_unique(f"td-comparison-{code}"),
            )
    return obj


def make_guide_section(*, author, languages=("en", "de")):
    guide = make_guide(author=author, languages=("en",))
    with translation.override("en"):
        section = GuideSection.objects.create(guide=guide, order=0)
        for code in languages:
            section.create_translation(code, title=f"Section {code}", body="sb")
    return section


#: ``(label, admin url prefix, translation model, factory)`` for the four
#: editorial roots. Both Author (own object) and Editor can open these change
#: forms, so both are valid subjects for a *link visibility* assertion.
ROOT_TRANSLATABLE_SURFACES = (
    ("prompt", "prompts_prompt", PromptTranslation, make_prompt),
    ("guide", "guides_guide", GuideTranslation, make_guide),
    ("usecase", "usecases_usecase", UseCaseTranslation, make_usecase),
    ("comparison", "compare_comparison", ComparisonTranslation, make_comparison),
)

#: ``GuideSectionAdmin`` gates its change form on Django's
#: ``guides.change_guidesection``, which the Author group does not hold - so
#: only the Editor can be measured for link visibility here (see
#: ``core/tests/test_editorial_delete_permissions.py``'s
#: ``GuideSectionAdminIsUnreachableForAuthorsTests``).
CHILD_TRANSLATABLE_SURFACES = (
    ("guidesection", "guides_guidesection", GuideSectionTranslation, make_guide_section),
)

#: Every translatable admin, for the URL-level checks whose answer is the same
#: for every non-privileged role regardless of change-form reachability.
ALL_TRANSLATABLE_SURFACES = ROOT_TRANSLATABLE_SURFACES + CHILD_TRANSLATABLE_SURFACES


def delete_translation_url(url_prefix, pk, language_code):
    return reverse(f"admin:{url_prefix}_delete_translation", args=[pk, language_code])


def change_url(url_prefix, pk):
    return reverse(f"admin:{url_prefix}_change", args=[pk])


def language_codes(obj):
    return sorted(obj.get_available_languages())


# ======================================================================
# 6.3 Author and Editor
# ======================================================================


class TranslationDeleteLinkHiddenForAuthorAndEditorTests(TestCase):
    """The rendered change form must not advertise a per-language delete link
    that the server would refuse anyway."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = make_staff_user("td-owner", group="Author")
        cls.editor = make_staff_user("td-editor", group="Editor")

    def _reachable(self):
        """``(label, url_prefix, factory, roles)``.

        Every surface is reachable for both roles: the four roots through
        ``EditorialWorkflowAdminMixin.has_change_permission``'s object-level
        branch, and ``GuideSection`` through Django's *view* permission, which
        renders a read-only form (status 200) rather than a 403. A read-only
        form still renders parler's language tabs, so the delete link must be
        suppressed there too."""
        for label, url_prefix, _tmodel, factory in ALL_TRANSLATABLE_SURFACES:
            yield label, url_prefix, factory, (
                ("author-owner", self.owner),
                ("editor", self.editor),
            )

    def test_no_delete_translation_link_is_rendered(self):
        for label, url_prefix, factory, roles in self._reachable():
            obj = factory(author=self.owner)
            self.assertEqual(language_codes(obj), ["de", "en"])
            for role, user in roles:
                with self.subTest(surface=label, role=role):
                    self.client.force_login(user)
                    resp = self.client.get(change_url(url_prefix, obj.pk))
                    self.assertEqual(resp.status_code, 200)
                    self.assertNotContains(resp, "delete-translation/")

    def test_language_tabs_disallow_deletion(self):
        for label, url_prefix, factory, roles in self._reachable():
            obj = factory(author=self.owner)
            for role, user in roles:
                with self.subTest(surface=label, role=role):
                    self.client.force_login(user)
                    resp = self.client.get(change_url(url_prefix, obj.pk))
                    self.assertFalse(resp.context["language_tabs"].allow_deletion)

    def test_language_tabs_are_still_rendered(self):
        """Hiding the delete link must not hide the language tabs themselves."""
        for label, url_prefix, factory, roles in self._reachable():
            obj = factory(author=self.owner)
            for role, user in roles:
                with self.subTest(surface=label, role=role):
                    self.client.force_login(user)
                    resp = self.client.get(change_url(url_prefix, obj.pk))
                    tabs = resp.context["language_tabs"]
                    self.assertEqual(
                        sorted(code for _url, _name, code, _status in tabs), ["de", "en"]
                    )


class TranslationDeleteRefusedForAuthorAndEditorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = make_staff_user("tdr-owner", group="Author")
        cls.other_author = make_staff_user("tdr-other", group="Author")
        cls.editor = make_staff_user("tdr-editor", group="Editor")

    #: Both refusal shapes are fail-closed and neither deletes anything.
    #: 403 is ``TranslatableAdmin.delete_translation``'s own
    #: ``has_delete_permission`` check; 404 happens one step earlier for a
    #: *foreign* ``GuideSection``, whose row ``ChildOfGuideOwnershipMixin.get_queryset``
    #: hides from a non-editor, so ``get_object()`` returns ``None`` and parler
    #: raises ``Http404`` before it ever reaches the permission check.
    REFUSED_STATUS_CODES = (403, 404)

    def _assert_refused(self, resp, tmodel, obj):
        self.assertIn(resp.status_code, self.REFUSED_STATUS_CODES)
        self.assertEqual(
            sorted(
                tmodel.objects.filter(master_id=obj.pk).values_list(
                    "language_code", flat=True
                )
            ),
            ["de", "en"],
        )

    def test_direct_get_is_refused_and_both_languages_survive(self):
        for label, url_prefix, tmodel, factory in ALL_TRANSLATABLE_SURFACES:
            obj = factory(author=self.owner)
            for role, user in (
                ("author-owner", self.owner),
                ("author-foreign", self.other_author),
                ("editor", self.editor),
            ):
                with self.subTest(surface=label, role=role):
                    self.client.force_login(user)
                    with silence_django_request_warnings():
                        resp = self.client.get(
                            delete_translation_url(url_prefix, obj.pk, "de")
                        )
                    self._assert_refused(resp, tmodel, obj)

    def test_direct_post_is_refused_and_both_languages_survive(self):
        for label, url_prefix, tmodel, factory in ALL_TRANSLATABLE_SURFACES:
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
                            delete_translation_url(url_prefix, obj.pk, "de"),
                            data={"post": "yes"},
                        )
                    self._assert_refused(resp, tmodel, obj)

    def test_an_owning_author_and_an_editor_are_refused_with_403(self):
        """The roles that *can* see the row get parler's own permission
        refusal, not an incidental 404 - so the denial is provably the
        permission check rather than queryset filtering."""
        for label, url_prefix, _tmodel, factory in ROOT_TRANSLATABLE_SURFACES:
            obj = factory(author=self.owner)
            for role, user in (("author-owner", self.owner), ("editor", self.editor)):
                with self.subTest(surface=label, role=role):
                    self.client.force_login(user)
                    with silence_django_request_warnings():
                        resp = self.client.post(
                            delete_translation_url(url_prefix, obj.pk, "de"),
                            data={"post": "yes"},
                        )
                    self.assertEqual(resp.status_code, 403)

    def test_root_row_is_untouched(self):
        guide = make_guide(author=self.owner)
        updated_before = Guide.objects.get(pk=guide.pk).updated_at
        self.client.force_login(self.owner)
        with silence_django_request_warnings():
            self.client.post(
                delete_translation_url("guides_guide", guide.pk, "de"),
                data={"post": "yes"},
            )
        after = Guide.objects.get(pk=guide.pk)
        self.assertEqual(after.updated_at, updated_before)
        self.assertEqual(after.status, Workflow.STATUS_DRAFT)

    def test_no_inline_translation_is_removed(self):
        tool = Tool.objects.create(slug=_unique("td-tool"))
        tool.create_translation("en", name="Tool")
        comparison = make_comparison(author=self.owner)
        entry = ComparisonToolEntry.objects.create(
            comparison=comparison, tool=tool, position=0
        )
        entry.create_translation("en", label="E en")
        entry.create_translation("de", label="E de")

        self.client.force_login(self.editor)
        with silence_django_request_warnings():
            self.client.post(
                delete_translation_url("compare_comparison", comparison.pk, "de"),
                data={"post": "yes"},
            )

        self.assertEqual(
            sorted(
                ComparisonToolEntryTranslation.objects.filter(
                    master_id=entry.pk
                ).values_list("language_code", flat=True)
            ),
            ["de", "en"],
        )

    def test_no_revision_and_no_deletion_log_entry_is_written(self):
        guide = make_guide(author=self.owner)
        revisions_before = Revision.objects.count()
        self.client.force_login(self.editor)
        with silence_django_request_warnings():
            self.client.post(
                delete_translation_url("guides_guide", guide.pk, "de"),
                data={"post": "yes"},
            )
        self.assertEqual(Revision.objects.count(), revisions_before)
        self.assertFalse(LogEntry.objects.filter(action_flag=DELETION).exists())


# ======================================================================
# 6.3 Admin group and superuser keep parler's contract
# ======================================================================


class TranslationDeleteAllowedForAdminAndSuperuserTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = make_staff_user("tda-owner", group="Author")
        cls.admin_group = make_staff_user("tda-admin", group="Admin")
        cls.superuser = make_staff_user("tda-su", superuser=True)

    def test_delete_translation_link_is_rendered(self):
        for role, user in (("admin-group", self.admin_group), ("superuser", self.superuser)):
            for label, url_prefix, _tmodel, factory in ALL_TRANSLATABLE_SURFACES:
                obj = factory(author=self.owner)
                with self.subTest(surface=label, role=role):
                    self.client.force_login(user)
                    resp = self.client.get(change_url(url_prefix, obj.pk))
                    self.assertEqual(resp.status_code, 200)
                    self.assertTrue(resp.context["language_tabs"].allow_deletion)
                    self.assertContains(resp, "delete-translation/")

    def test_direct_get_renders_parlers_confirmation(self):
        for role, user in (("admin-group", self.admin_group), ("superuser", self.superuser)):
            for label, url_prefix, tmodel, factory in ROOT_TRANSLATABLE_SURFACES:
                obj = factory(author=self.owner)
                with self.subTest(surface=label, role=role):
                    self.client.force_login(user)
                    resp = self.client.get(
                        delete_translation_url(url_prefix, obj.pk, "de")
                    )
                    self.assertEqual(resp.status_code, 200)
                    self.assertEqual(
                        tmodel.objects.filter(master_id=obj.pk).count(), 2
                    )

    def test_confirmed_post_removes_only_that_language(self):
        for role, user in (("admin-group", self.admin_group), ("superuser", self.superuser)):
            for label, url_prefix, tmodel, factory in ROOT_TRANSLATABLE_SURFACES:
                obj = factory(author=self.owner)
                with self.subTest(surface=label, role=role):
                    self.client.force_login(user)
                    resp = self.client.post(
                        delete_translation_url(url_prefix, obj.pk, "de"),
                        data={"post": "yes"},
                    )
                    self.assertEqual(resp.status_code, 302)
                    self.assertEqual(
                        list(
                            tmodel.objects.filter(master_id=obj.pk).values_list(
                                "language_code", flat=True
                            )
                        ),
                        ["en"],
                    )

    def test_last_remaining_language_stays_protected(self):
        guide = make_guide(author=self.owner, languages=("en",))
        self.client.force_login(self.superuser)
        resp = self.client.post(
            delete_translation_url("guides_guide", guide.pk, "en"),
            data={"post": "yes"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            list(
                GuideTranslation.objects.filter(master_id=guide.pk).values_list(
                    "language_code", flat=True
                )
            ),
            ["en"],
        )

    def test_single_language_object_offers_no_delete_link(self):
        guide = make_guide(author=self.owner, languages=("en",))
        self.client.force_login(self.superuser)
        resp = self.client.get(change_url("guides_guide", guide.pk))
        self.assertFalse(resp.context["language_tabs"].allow_deletion)
        self.assertNotContains(resp, "delete-translation/")

    def test_translated_inline_children_follow_parlers_existing_contract(self):
        """``delete_inline_translations`` is ``True`` by default, so deleting a
        comparison's German translation also drops its entries' German rows.
        D3B must not change that for Admin/superuser."""
        tool = Tool.objects.create(slug=_unique("tda-tool"))
        tool.create_translation("en", name="Tool")
        comparison = make_comparison(author=self.owner)
        entry = ComparisonToolEntry.objects.create(
            comparison=comparison, tool=tool, position=0
        )
        entry.create_translation("en", label="E en")
        entry.create_translation("de", label="E de")

        self.client.force_login(self.admin_group)
        resp = self.client.post(
            delete_translation_url("compare_comparison", comparison.pk, "de"),
            data={"post": "yes"},
        )
        self.assertEqual(resp.status_code, 302)

        self.assertEqual(
            list(
                ComparisonTranslation.objects.filter(
                    master_id=comparison.pk
                ).values_list("language_code", flat=True)
            ),
            ["en"],
        )
        self.assertEqual(
            list(
                ComparisonToolEntryTranslation.objects.filter(
                    master_id=entry.pk
                ).values_list("language_code", flat=True)
            ),
            ["en"],
        )
        self.assertTrue(ComparisonToolEntry.objects.filter(pk=entry.pk).exists())
        self.assertTrue(Comparison.objects.filter(pk=comparison.pk).exists())


# ======================================================================
# GuideSection: the one surface whose parent has to be resolved through
# the translation's ``master``
# ======================================================================


class GuideSectionTranslationDeleteAllowedForAdminAndSuperuserTests(TestCase):
    """``TranslatableAdmin.delete_translation()`` hands
    ``has_delete_permission()`` the *translation* row, not the section. A
    ``GuideSectionTranslation`` reaches its guide as
    ``translation.master.guide``, so ``ChildOfGuideOwnershipMixin`` has to
    resolve that chain - otherwise even Admin and superuser are refused a
    delete they are explicitly entitled to."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = make_staff_user("gsta-owner", group="Author")
        cls.admin_group = make_staff_user("gsta-admin", group="Admin")
        cls.superuser = make_staff_user("gsta-su", superuser=True)

    def test_delete_translation_link_is_rendered(self):
        for role, user in (("admin-group", self.admin_group), ("superuser", self.superuser)):
            section = make_guide_section(author=self.owner)
            with self.subTest(role=role):
                self.client.force_login(user)
                resp = self.client.get(change_url("guides_guidesection", section.pk))
                self.assertEqual(resp.status_code, 200)
                self.assertTrue(resp.context["language_tabs"].allow_deletion)
                self.assertContains(resp, "delete-translation/")

    def test_direct_get_renders_parlers_confirmation(self):
        for role, user in (("admin-group", self.admin_group), ("superuser", self.superuser)):
            section = make_guide_section(author=self.owner)
            with self.subTest(role=role):
                self.client.force_login(user)
                resp = self.client.get(
                    delete_translation_url("guides_guidesection", section.pk, "de")
                )
                self.assertNotEqual(resp.status_code, 403)
                self.assertEqual(resp.status_code, 200)
                self.assertIn("deleted_objects", resp.context)
                self.assertEqual(
                    GuideSectionTranslation.objects.filter(master_id=section.pk).count(), 2
                )

    def test_confirmed_post_removes_only_the_selected_language(self):
        for role, user in (("admin-group", self.admin_group), ("superuser", self.superuser)):
            section = make_guide_section(author=self.owner)
            guide = section.guide
            with self.subTest(role=role):
                self.client.force_login(user)
                resp = self.client.post(
                    delete_translation_url("guides_guidesection", section.pk, "de"),
                    data={"post": "yes"},
                )
                self.assertEqual(resp.status_code, 302)
                self.assertEqual(
                    list(
                        GuideSectionTranslation.objects.filter(
                            master_id=section.pk
                        ).values_list("language_code", flat=True)
                    ),
                    ["en"],
                )
                self.assertTrue(GuideSection.objects.filter(pk=section.pk).exists())
                self.assertTrue(Guide.objects.filter(pk=guide.pk).exists())

    def test_section_and_guide_are_otherwise_untouched(self):
        """No archive workflow is triggered and no other field moves."""
        section = make_guide_section(author=self.owner)
        guide = section.guide
        order_before = section.order
        guide_before = Guide.objects.get(pk=guide.pk)

        self.client.force_login(self.admin_group)
        self.client.post(
            delete_translation_url("guides_guidesection", section.pk, "de"),
            data={"post": "yes"},
        )

        section_after = GuideSection.objects.get(pk=section.pk)
        guide_after = Guide.objects.get(pk=guide.pk)
        self.assertEqual(section_after.order, order_before)
        self.assertEqual(section_after.guide_id, guide.pk)
        self.assertEqual(guide_after.status, guide_before.status)
        self.assertEqual(guide_after.is_published, guide_before.is_published)
        self.assertEqual(guide_after.live_i18n, guide_before.live_i18n)

    def test_last_remaining_language_stays_protected_afterwards(self):
        section = make_guide_section(author=self.owner)
        self.client.force_login(self.superuser)

        first = self.client.post(
            delete_translation_url("guides_guidesection", section.pk, "de"),
            data={"post": "yes"},
        )
        self.assertEqual(first.status_code, 302)

        second = self.client.post(
            delete_translation_url("guides_guidesection", section.pk, "en"),
            data={"post": "yes"},
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            list(
                GuideSectionTranslation.objects.filter(master_id=section.pk).values_list(
                    "language_code", flat=True
                )
            ),
            ["en"],
        )

    def test_single_language_section_offers_no_delete_link(self):
        section = make_guide_section(author=self.owner, languages=("en",))
        self.client.force_login(self.admin_group)
        resp = self.client.get(change_url("guides_guidesection", section.pk))
        self.assertFalse(resp.context["language_tabs"].allow_deletion)
        self.assertNotContains(resp, "delete-translation/")


class GuideSectionTranslationDeleteRefusalCodesTests(TestCase):
    """The exact refusal each non-privileged path produces, so the two
    different fail-closed shapes stay distinguishable:

    * **403** - the row is visible to the requester, so parler reaches
      ``has_delete_permission()`` and that returns ``False`` (no
      ``guides.delete_guidesection``). This is the Author-on-own-section and
      the Editor path.
    * **404** - the row is *not* visible: ``ChildOfGuideOwnershipMixin.get_queryset``
      restricts a non-editor to ``guide__author=request.user``, so
      ``get_object()`` returns ``None`` and parler raises ``Http404`` before
      any permission check. This is the Author-on-foreign-section path.
    """

    @classmethod
    def setUpTestData(cls):
        cls.owner = make_staff_user("gstr-owner", group="Author")
        cls.other_author = make_staff_user("gstr-other", group="Author")
        cls.editor = make_staff_user("gstr-editor", group="Editor")

    def _assert_intact(self, section):
        self.assertEqual(
            sorted(
                GuideSectionTranslation.objects.filter(master_id=section.pk).values_list(
                    "language_code", flat=True
                )
            ),
            ["de", "en"],
        )
        self.assertTrue(GuideSection.objects.filter(pk=section.pk).exists())
        self.assertTrue(Guide.objects.filter(pk=section.guide_id).exists())

    def test_owning_author_and_editor_get_403_on_get_and_post(self):
        for role, user in (("author-owner", self.owner), ("editor", self.editor)):
            section = make_guide_section(author=self.owner)
            url = delete_translation_url("guides_guidesection", section.pk, "de")
            with self.subTest(role=role):
                self.client.force_login(user)
                with silence_django_request_warnings():
                    get_resp = self.client.get(url)
                    post_resp = self.client.post(url, data={"post": "yes"})
                self.assertEqual(get_resp.status_code, 403)
                self.assertEqual(post_resp.status_code, 403)
                self._assert_intact(section)

    def test_foreign_author_gets_404_on_get_and_post(self):
        section = make_guide_section(author=self.owner)
        url = delete_translation_url("guides_guidesection", section.pk, "de")
        self.client.force_login(self.other_author)
        with silence_django_request_warnings():
            get_resp = self.client.get(url)
            post_resp = self.client.post(url, data={"post": "yes"})
        self.assertEqual(get_resp.status_code, 404)
        self.assertEqual(post_resp.status_code, 404)
        self._assert_intact(section)

    def test_no_delete_link_is_offered_to_either_role(self):
        section = make_guide_section(author=self.owner)
        for role, user in (("author-owner", self.owner), ("editor", self.editor)):
            with self.subTest(role=role):
                self.client.force_login(user)
                resp = self.client.get(change_url("guides_guidesection", section.pk))
                self.assertEqual(resp.status_code, 200)
                self.assertFalse(resp.context["language_tabs"].allow_deletion)
                self.assertNotContains(resp, "delete-translation/")


class ParentGuideResolutionTests(TestCase):
    """Unit-level companion to the integration tests above - never their
    replacement. Exercises the real registered ``GuideSectionAdmin``."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = make_staff_user("pgr-owner", group="Author")
        cls.section = make_guide_section(author=cls.owner)

    def _admin(self):
        from django.contrib import admin as django_admin

        return django_admin.site._registry[GuideSection]

    def test_a_section_resolves_to_its_guide(self):
        self.assertEqual(
            self._admin()._get_parent_guide(self.section), self.section.guide
        )

    def test_a_section_translation_resolves_to_the_same_guide(self):
        translation = GuideSectionTranslation.objects.get(
            master_id=self.section.pk, language_code="de"
        )
        self.assertEqual(
            self._admin()._get_parent_guide(translation), self.section.guide
        )

    def test_none_resolves_to_none(self):
        self.assertIsNone(self._admin()._get_parent_guide(None))

    def test_an_unrelated_object_resolves_to_none(self):
        self.assertIsNone(self._admin()._get_parent_guide(object()))

    def test_a_translation_without_a_master_is_fail_closed(self):
        """``master`` is a non-nullable FK, so an unsaved row raises
        ``RelatedObjectDoesNotExist`` - which subclasses ``AttributeError``
        and is therefore absorbed into ``None`` rather than a 500."""
        self.assertIsNone(self._admin()._get_parent_guide(GuideSectionTranslation()))

    def test_a_section_without_a_guide_is_fail_closed(self):
        self.assertIsNone(self._admin()._get_parent_guide(GuideSection()))
