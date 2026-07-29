"""
Beta 11.11D3B: the role-bound deletion contract for editorial content -
admin inline (formset) deletion of structural children.

The three inline surfaces that can remove editorial structure are:

* ``GuideSectionInline``       under ``GuideAdmin``            (prefix ``sections``)
* ``GuideItemInline``          under ``GuideSectionAdmin``     (prefix ``items``)
* ``ComparisonToolEntryInline`` under ``ComparisonAdmin``      (prefix ``tool_entries``)

Django builds each inline formset with
``can_delete = self.can_delete and self.has_delete_permission(request, obj)``
(``ModelAdmin`` / ``InlineModelAdmin.get_formset``), and
``InlineModelAdmin.has_delete_permission`` resolves - for a non-auto-created
model - to the ordinary ``<app>.delete_<model>`` permission. Author and Editor
hold none of those, so their formsets are constructed with
``can_delete=False``: ``BaseFormSet.add_fields`` never adds the
``DELETION_FIELD_NAME`` field, and ``deleted_forms`` short-circuits on the same
flag. A forged ``...-DELETE=on`` is therefore inert rather than merely
unrendered - which is what these tests prove, by sending exactly that together
with a *real* content change on the **parent** so a silently rejected form
cannot make the assertion pass vacuously.

The control change has to sit on the parent rather than on the child: neither
role holds ``change_<child>`` either, and Django's
``DeleteProtectedModelForm.has_changed()`` returns ``False`` for an existing
row whenever ``can_change`` is false - so an Author's or Editor's child edits
are discarded as well. Only the parent field is genuinely writable for them.

Admin group and superuser keep the checkbox and the deletion.

``GuideSectionAdmin`` (the ``GuideItemInline`` host) admits an Author through
Django's *view* permission and renders a read-only form; a POST to it is
refused with 403. Both facts are asserted below.
"""
import itertools

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from catalog.models import Tool
from compare.models import Comparison, ComparisonToolEntry
from core.models.editorial import EditorialWorkflowMixin as Workflow
from guides.models import Guide, GuideItem, GuideSection
from mentoroai.tests.utils import silence_django_request_warnings

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


def formset_by_prefix(response, prefix):
    for inline_admin_formset in response.context["inline_admin_formsets"]:
        if inline_admin_formset.formset.prefix == prefix:
            return inline_admin_formset.formset
    raise AssertionError(f"no inline formset with prefix {prefix!r} in the response")


# ======================================================================
# Guide -> GuideSectionInline
# ======================================================================


class GuideSectionInlineDeletePermissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = make_staff_user("gsi-owner", group="Author")
        cls.editor = make_staff_user("gsi-editor", group="Editor")
        cls.admin_group = make_staff_user("gsi-admin", group="Admin")
        cls.superuser = make_staff_user("gsi-su", superuser=True)

    def setUp(self):
        with translation.override("en"):
            self.guide = Guide.objects.create(
                author=self.owner, status=Workflow.STATUS_DRAFT
            )
            self.slug = _unique("gsi-guide")
            self.guide.create_translation(
                "en", title="Guide EN", intro="i", body="b", slug=self.slug
            )
            self.section = GuideSection.objects.create(guide=self.guide, order=0)
            self.section.create_translation("en", title="Section EN", body="sb")
        self.change_url = reverse("admin:guides_guide_change", args=[self.guide.pk])

    def _payload(self, *, section_title="Section EN", guide_intro="i", delete=False):
        data = {
            "author": str(self.owner.pk),
            "review_note": "",
            "published_at_0": "",
            "published_at_1": "",
            "slug": self.slug,
            "title": "Guide EN",
            "intro": guide_intro,
            "body": "b",
            "sections-TOTAL_FORMS": "1",
            "sections-INITIAL_FORMS": "1",
            "sections-MIN_NUM_FORMS": "0",
            "sections-MAX_NUM_FORMS": "1000",
            "sections-0-id": str(self.section.pk),
            "sections-0-guide": str(self.guide.pk),
            "sections-0-order": "0",
            "sections-0-title": section_title,
            "sections-0-body": "sb",
            "_continue": "Save",
        }
        if delete:
            data["sections-0-DELETE"] = "on"
        return data

    def test_author_and_editor_get_a_formset_without_deletion(self):
        for role, user in (("author-owner", self.owner), ("editor", self.editor)):
            with self.subTest(role=role):
                self.client.force_login(user)
                resp = self.client.get(self.change_url)
                self.assertEqual(resp.status_code, 200)
                self.assertFalse(formset_by_prefix(resp, "sections").can_delete)
                self.assertNotContains(resp, "sections-0-DELETE")

    def test_forged_delete_flag_does_not_remove_the_section(self):
        for role, user in (("author-owner", self.owner), ("editor", self.editor)):
            with self.subTest(role=role):
                self.client.force_login(user)
                marker = f"intro changed by {role}"
                resp = self.client.post(
                    self.change_url, data=self._payload(guide_intro=marker, delete=True)
                )
                self.assertEqual(resp.status_code, 302)
                # The parent-level control change proves the POST really was
                # accepted and saved - so the surviving section below is the
                # guard working, not a silently rejected form. The control has
                # to sit on the *parent*: for an Author the whole inline is
                # read-only (no ``guides.change_guidesection``), and Django's
                # ``DeleteProtectedModelForm.has_changed()`` discards child
                # edits in that case.
                self.assertEqual(
                    Guide.objects.get(pk=self.guide.pk).safe_translation_getter(
                        "intro", language_code="en"
                    ),
                    marker,
                )
                self.assertTrue(GuideSection.objects.filter(pk=self.section.pk).exists())
                self.assertEqual(self.guide.sections.count(), 1)

    def test_admin_and_superuser_get_the_delete_checkbox(self):
        for role, user in (("admin-group", self.admin_group), ("superuser", self.superuser)):
            with self.subTest(role=role):
                self.client.force_login(user)
                resp = self.client.get(self.change_url)
                self.assertEqual(resp.status_code, 200)
                self.assertTrue(formset_by_prefix(resp, "sections").can_delete)
                self.assertContains(resp, "sections-0-DELETE")

    def test_admin_can_delete_the_section_through_the_inline(self):
        self.client.force_login(self.admin_group)
        resp = self.client.post(self.change_url, data=self._payload(delete=True))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(GuideSection.objects.filter(pk=self.section.pk).exists())
        self.assertTrue(Guide.objects.filter(pk=self.guide.pk).exists())

    def test_superuser_can_delete_the_section_through_the_inline(self):
        self.client.force_login(self.superuser)
        resp = self.client.post(self.change_url, data=self._payload(delete=True))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(GuideSection.objects.filter(pk=self.section.pk).exists())


# ======================================================================
# GuideSection -> GuideItemInline
# ======================================================================


class GuideItemInlineDeletePermissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = make_staff_user("gii-owner", group="Author")
        cls.editor = make_staff_user("gii-editor", group="Editor")
        cls.admin_group = make_staff_user("gii-admin", group="Admin")
        cls.superuser = make_staff_user("gii-su", superuser=True)

    def setUp(self):
        with translation.override("en"):
            self.guide = Guide.objects.create(
                author=self.owner, status=Workflow.STATUS_DRAFT
            )
            self.guide.create_translation(
                "en", title="Guide EN", intro="i", body="b", slug=_unique("gii-guide")
            )
            self.section = GuideSection.objects.create(guide=self.guide, order=0)
            self.section.create_translation("en", title="Section EN", body="sb")
            self.item = GuideItem.objects.create(
                section=self.section, kind="guide", order=0, url="https://example.com/a"
            )
            self.item.create_translation("en", title="Item EN", teaser="t")
        self.change_url = reverse(
            "admin:guides_guidesection_change", args=[self.section.pk]
        )

    def _payload(self, *, item_title, delete=False):
        data = {
            "guide": str(self.guide.pk),
            "order": "0",
            "title": "Section EN",
            "body": "sb",
            "items-TOTAL_FORMS": "1",
            "items-INITIAL_FORMS": "1",
            "items-MIN_NUM_FORMS": "0",
            "items-MAX_NUM_FORMS": "1000",
            "items-0-id": str(self.item.pk),
            "items-0-section": str(self.section.pk),
            "items-0-order": "0",
            "items-0-is_published": "on",
            "items-0-kind": "guide",
            "items-0-content_type": "",
            "items-0-object_id": "",
            "items-0-url": "https://example.com/a",
            "items-0-title": item_title,
            "items-0-teaser": "t",
            "_continue": "Save",
        }
        if delete:
            data["items-0-DELETE"] = "on"
        return data

    def test_editor_gets_a_formset_without_deletion(self):
        self.client.force_login(self.editor)
        resp = self.client.get(self.change_url)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(formset_by_prefix(resp, "items").can_delete)
        self.assertNotContains(resp, "items-0-DELETE")

    def test_author_gets_a_read_only_surface_without_deletion(self):
        """The Author group holds ``guides.view_guidesection`` but not
        ``change_``, so the GET renders read-only and the POST is refused
        outright - two independent reasons the item cannot be deleted."""
        self.client.force_login(self.owner)
        resp = self.client.get(self.change_url)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context["has_change_permission"])
        self.assertFalse(formset_by_prefix(resp, "items").can_delete)
        self.assertNotContains(resp, "items-0-DELETE")

        with silence_django_request_warnings():
            post = self.client.post(
                self.change_url, data=self._payload(item_title="Changed", delete=True)
            )
        self.assertEqual(post.status_code, 403)
        self.assertTrue(GuideItem.objects.filter(pk=self.item.pk).exists())

    def test_forged_delete_flag_does_not_remove_the_item(self):
        self.client.force_login(self.editor)
        resp = self.client.post(
            self.change_url, data=self._payload(item_title="Changed", delete=True)
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(GuideItem.objects.filter(pk=self.item.pk).exists())
        self.assertEqual(
            GuideItem.objects.get(pk=self.item.pk).safe_translation_getter(
                "title", language_code="en"
            ),
            "Changed",
        )
        self.assertEqual(self.section.items.count(), 1)

    def test_admin_and_superuser_get_the_delete_checkbox(self):
        for role, user in (("admin-group", self.admin_group), ("superuser", self.superuser)):
            with self.subTest(role=role):
                self.client.force_login(user)
                resp = self.client.get(self.change_url)
                self.assertEqual(resp.status_code, 200)
                self.assertTrue(formset_by_prefix(resp, "items").can_delete)
                self.assertContains(resp, "items-0-DELETE")

    def test_admin_can_delete_the_item_through_the_inline(self):
        self.client.force_login(self.admin_group)
        resp = self.client.post(
            self.change_url, data=self._payload(item_title="Item EN", delete=True)
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(GuideItem.objects.filter(pk=self.item.pk).exists())
        self.assertTrue(GuideSection.objects.filter(pk=self.section.pk).exists())


# ======================================================================
# Comparison -> ComparisonToolEntryInline
# ======================================================================


class ComparisonToolEntryInlineDeletePermissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = make_staff_user("ctei-owner", group="Author")
        cls.editor = make_staff_user("ctei-editor", group="Editor")
        cls.admin_group = make_staff_user("ctei-admin", group="Admin")
        cls.superuser = make_staff_user("ctei-su", superuser=True)

    def setUp(self):
        with translation.override("en"):
            self.tool = Tool.objects.create(slug=_unique("ctei-tool"))
            self.tool.create_translation("en", name="Tool")
            self.comparison = Comparison.objects.create(
                author=self.owner, status=Workflow.STATUS_DRAFT
            )
            self.slug = _unique("ctei-cmp")
            self.comparison.create_translation(
                "en", title="CMP EN", intro="i", body="b", slug=self.slug
            )
            self.entry = ComparisonToolEntry.objects.create(
                comparison=self.comparison, tool=self.tool, position=0
            )
            self.entry.create_translation("en", label="Entry EN", summary="s")
        self.change_url = reverse(
            "admin:compare_comparison_change", args=[self.comparison.pk]
        )

    def _payload(self, *, entry_label="Entry EN", comparison_intro="i", delete=False):
        data = {
            "author": str(self.owner.pk),
            "review_note": "",
            "slug": self.slug,
            "title": "CMP EN",
            "intro": comparison_intro,
            "body": "b",
            "tool_entries-TOTAL_FORMS": "1",
            "tool_entries-INITIAL_FORMS": "1",
            "tool_entries-MIN_NUM_FORMS": "0",
            "tool_entries-MAX_NUM_FORMS": "1000",
            "tool_entries-0-id": str(self.entry.pk),
            "tool_entries-0-comparison": str(self.comparison.pk),
            "tool_entries-0-tool": str(self.tool.pk),
            "tool_entries-0-position": "0",
            "tool_entries-0-label": entry_label,
            "tool_entries-0-summary": "s",
            "tool_entries-0-pros": "",
            "tool_entries-0-cons": "",
            "tool_entries-0-special": "",
            "_continue": "Save",
        }
        if delete:
            data["tool_entries-0-DELETE"] = "on"
        return data

    def test_author_and_editor_get_a_formset_without_deletion(self):
        for role, user in (("author-owner", self.owner), ("editor", self.editor)):
            with self.subTest(role=role):
                self.client.force_login(user)
                resp = self.client.get(self.change_url)
                self.assertEqual(resp.status_code, 200)
                self.assertFalse(formset_by_prefix(resp, "tool_entries").can_delete)
                self.assertNotContains(resp, "tool_entries-0-DELETE")

    def test_forged_delete_flag_does_not_remove_the_entry(self):
        for role, user in (("author-owner", self.owner), ("editor", self.editor)):
            with self.subTest(role=role):
                self.client.force_login(user)
                marker = f"intro changed by {role}"
                resp = self.client.post(
                    self.change_url,
                    data=self._payload(comparison_intro=marker, delete=True),
                )
                self.assertEqual(resp.status_code, 302)
                # Parent-level control change - see the guide-section variant
                # for why the control cannot sit on the child.
                self.assertEqual(
                    Comparison.objects.get(pk=self.comparison.pk).safe_translation_getter(
                        "intro", language_code="en"
                    ),
                    marker,
                )
                self.assertTrue(
                    ComparisonToolEntry.objects.filter(pk=self.entry.pk).exists()
                )
                self.assertEqual(self.comparison.tool_entries.count(), 1)

    def test_admin_and_superuser_get_the_delete_checkbox(self):
        for role, user in (("admin-group", self.admin_group), ("superuser", self.superuser)):
            with self.subTest(role=role):
                self.client.force_login(user)
                resp = self.client.get(self.change_url)
                self.assertEqual(resp.status_code, 200)
                self.assertTrue(formset_by_prefix(resp, "tool_entries").can_delete)
                self.assertContains(resp, "tool_entries-0-DELETE")

    def test_admin_can_delete_the_entry_through_the_inline(self):
        self.client.force_login(self.admin_group)
        resp = self.client.post(
            self.change_url, data=self._payload(delete=True)
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(ComparisonToolEntry.objects.filter(pk=self.entry.pk).exists())
        self.assertTrue(Comparison.objects.filter(pk=self.comparison.pk).exists())

    def test_superuser_can_delete_the_entry_through_the_inline(self):
        self.client.force_login(self.superuser)
        resp = self.client.post(
            self.change_url, data=self._payload(delete=True)
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(ComparisonToolEntry.objects.filter(pk=self.entry.pk).exists())
