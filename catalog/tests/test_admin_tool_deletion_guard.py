"""
Beta 11.11D4C: the Django admin refuses to hard-delete a tool that a
``ComparisonToolEntry`` still references.

Why this exists. ``ComparisonToolEntry.tool`` is ``on_delete=CASCADE``, and an
entry is not a join row - it carries its own editorial content (``position``
plus the translated ``label``/``summary``/``pros``/``cons``/``special`` fields,
in every language). Reproduced before the fix, inside a rolled-back
transaction: deleting one tool that had one entry with two translations removed
the tool (1->0), the entry (1->0) **and both entry translations (2->0)**, while
the comparison itself survived and the public projection fell back to zero
entries. Nothing warned the operator, and the loss is unrecoverable.

What D4C is, and is not:

* It guards the two Django admin deletion paths - the single-object delete view
  and the ``delete_selected`` changelist action - by adding the offending tools
  to the ``protected`` list ``ModelAdmin.get_deleted_objects()`` returns. Both
  paths already consult that list *before* deciding to delete
  (``if request.POST and not protected`` / ``if request.POST.get("post") and
  not protected``), so a protected selection is blocked atomically, renders
  Django's own "Cannot delete" page, and never produces a success message.
* It is **not** a model, manager, queryset or signal guard, and it changes no
  ``on_delete``. A direct ``tool.delete()`` outside the admin still cascades
  exactly as before - pinned by :class:`DirectOrmDeleteStillCascadesTests` so
  the boundary is explicit rather than assumed.

Plain M2M links (``Prompt.tools``, ``UseCase.tools``, ``Guide.tools``) carry no
editorial payload of their own and therefore never block, and ``PricingTier`` /
``AffiliateProgram`` stay ordinary cascade children of the tool.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from catalog.models import AffiliateProgram, PricingTier, Tool
from compare.models import (
    Comparison,
    ComparisonToolEntry,
    ComparisonToolEntryTranslation,
)
from guides.models import Guide
from prompts.models import Prompt
from usecases.models import UseCase

User = get_user_model()


def make_tool(slug, *, name=None):
    tool = Tool.objects.create(slug=slug, published_at=timezone.now())
    tool.create_translation("en", name=name or f"Tool {slug}", short_description="s")
    return tool


def make_comparison(slug):
    comparison = Comparison.objects.create()
    comparison.create_translation("en", title=f"Cmp {slug}", slug=slug)
    return comparison


def add_entry(comparison, tool, *, position=10):
    entry = ComparisonToolEntry.objects.create(
        comparison=comparison, tool=tool, position=position
    )
    for language in ("en", "de"):
        entry.create_translation(
            language,
            label=f"{language} label",
            summary=f"{language} summary",
            pros=f"{language} pros",
            cons=f"{language} cons",
            special=f"{language} special",
        )
    return entry


class ToolDeletionGuardTestCase(TestCase):
    """Deletion is an Admin/superuser capability in this project; Author and
    Editor have no ToolAdmin access at all (asserted in
    :class:`RolesAreUnchangedTests`)."""

    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser(
            username="d4c-super", email="d4c-super@example.com", password="pw"
        )
        cls.admin_group_user = User.objects.create_user(
            username="d4c-admin", password="pw", is_staff=True
        )
        cls.admin_group_user.groups.add(Group.objects.get(name="Admin"))

    def setUp(self):
        super().setUp()
        self.client.force_login(self.superuser)

    # -- helpers -------------------------------------------------------

    def delete_url(self, tool):
        return reverse("admin:catalog_tool_delete", args=[tool.pk])

    def changelist_url(self):
        return reverse("admin:catalog_tool_changelist")

    def bulk_delete(self, tools, *, confirm=True):
        data = {
            "action": "delete_selected",
            "_selected_action": [str(tool.pk) for tool in tools],
        }
        if confirm:
            data["post"] = "yes"
        return self.client.post(self.changelist_url(), data, follow=True)


class UnreferencedToolStaysDeletableTests(ToolDeletionGuardTestCase):
    def test_the_confirmation_page_offers_the_normal_delete(self):
        tool = make_tool("d4c-free")
        resp = self.client.get(self.delete_url(tool))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Are you sure")

    def test_a_confirmed_post_deletes_the_tool(self):
        tool = make_tool("d4c-free-post")
        resp = self.client.post(self.delete_url(tool), {"post": "yes"}, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Tool.objects.filter(pk=tool.pk).exists())

    def test_an_admin_group_user_may_also_delete_an_unreferenced_tool(self):
        tool = make_tool("d4c-free-admin")
        self.client.force_login(self.admin_group_user)
        self.client.post(self.delete_url(tool), {"post": "yes"}, follow=True)
        self.assertFalse(Tool.objects.filter(pk=tool.pk).exists())

    def test_dependent_tool_data_still_cascades_on_an_allowed_delete(self):
        """PricingTier/AffiliateProgram are the tool's own dependent rows, not
        editorial content of another object - they keep cascading."""
        tool = make_tool("d4c-free-children")
        PricingTier.objects.create(tool=tool)
        AffiliateProgram.objects.create(tool=tool, network="d4c-net")

        self.client.post(self.delete_url(tool), {"post": "yes"}, follow=True)

        self.assertFalse(Tool.objects.filter(pk=tool.pk).exists())
        self.assertFalse(PricingTier.objects.filter(tool_id=tool.pk).exists())
        self.assertFalse(AffiliateProgram.objects.filter(tool_id=tool.pk).exists())


class ReferencedToolIsProtectedTests(ToolDeletionGuardTestCase):
    def setUp(self):
        super().setUp()
        self.tool = make_tool("d4c-referenced", name="Referenced Tool")
        self.comparison = make_comparison("d4c-cmp")
        self.entry = add_entry(self.comparison, self.tool)

    def assert_nothing_was_deleted(self):
        self.assertTrue(Tool.objects.filter(pk=self.tool.pk).exists())
        self.assertTrue(Comparison.objects.filter(pk=self.comparison.pk).exists())
        self.assertTrue(ComparisonToolEntry.objects.filter(pk=self.entry.pk).exists())
        self.assertEqual(
            ComparisonToolEntryTranslation.objects.filter(master_id=self.entry.pk).count(),
            2,
        )
        entry = ComparisonToolEntry.objects.get(pk=self.entry.pk)
        self.assertEqual(entry.position, 10)
        self.assertEqual(
            entry.safe_translation_getter("summary", language_code="de"), "de summary"
        )

    def test_the_delete_page_explains_the_block_instead_of_offering_deletion(self):
        resp = self.client.get(self.delete_url(self.tool))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "protected related objects")
        self.assertNotContains(resp, "Are you sure")
        self.assert_nothing_was_deleted()

    def test_a_confirmed_post_deletes_nothing(self):
        """The delete button is not even rendered - this posts anyway, exactly
        as a hand-crafted request would."""
        resp = self.client.post(self.delete_url(self.tool), {"post": "yes"})
        self.assertEqual(resp.status_code, 200)
        self.assert_nothing_was_deleted()

    def test_the_reason_appears_in_the_protected_list_not_just_the_cascade_preview(self):
        """Django lists cascade children anyway; what matters is that the entry
        is in ``protected``, because that is the list both delete paths check
        before deciding to delete."""
        resp = self.client.get(self.delete_url(self.tool))
        protected = resp.context["protected"]
        self.assertTrue(protected, "nothing was marked protected")
        self.assertTrue(
            any("Tool comparison entry" in str(item) for item in protected),
            f"protected list does not name the blocking entry: {protected}",
        )

    def test_no_500_and_no_permission_denied(self):
        for method in ("get", "post"):
            with self.subTest(method=method):
                resp = getattr(self.client, method)(
                    self.delete_url(self.tool), {"post": "yes"} if method == "post" else None
                )
                self.assertEqual(resp.status_code, 200)


class BulkDeletionGuardTests(ToolDeletionGuardTestCase):
    def test_a_selection_of_only_unreferenced_tools_is_deleted(self):
        tools = [make_tool(f"d4c-bulk-free-{i}") for i in range(3)]
        resp = self.bulk_delete(tools)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Tool.objects.filter(pk__in=[t.pk for t in tools]).exists())
        self.assertContains(resp, "Successfully deleted")

    def test_m2m_only_links_never_block_a_bulk_delete(self):
        tool = make_tool("d4c-bulk-m2m")
        prompt = Prompt.objects.create()
        prompt.create_translation("en", title="P", intro="i", body="b", outro="o", slug="d4c-p")
        prompt.tools.add(tool)
        usecase = UseCase.objects.create()
        usecase.create_translation(
            "en", title="U", intro="i", body="b", outro="o", slug="d4c-u", persona="P"
        )
        usecase.tools.add(tool)
        guide = Guide.objects.create()
        guide.create_translation("en", title="G", slug="d4c-g")
        guide.tools.add(tool)

        self.bulk_delete([tool])

        self.assertFalse(Tool.objects.filter(pk=tool.pk).exists())
        self.assertTrue(Prompt.objects.filter(pk=prompt.pk).exists())
        self.assertTrue(UseCase.objects.filter(pk=usecase.pk).exists())
        self.assertTrue(Guide.objects.filter(pk=guide.pk).exists())
        self.assertEqual(prompt.tools.count(), 0)
        self.assertEqual(usecase.tools.count(), 0)
        self.assertEqual(guide.tools.count(), 0)

    def test_a_mixed_selection_is_blocked_atomically(self):
        """The unreferenced tool in the same selection must survive too - a
        partial bulk delete after a confirmed POST would be unpredictable."""
        free = make_tool("d4c-bulk-mixed-free")
        referenced = make_tool("d4c-bulk-mixed-ref")
        comparison = make_comparison("d4c-bulk-cmp")
        entry = add_entry(comparison, referenced)

        resp = self.bulk_delete([free, referenced])

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Tool.objects.filter(pk=free.pk).exists())
        self.assertTrue(Tool.objects.filter(pk=referenced.pk).exists())
        self.assertTrue(ComparisonToolEntry.objects.filter(pk=entry.pk).exists())
        self.assertEqual(
            ComparisonToolEntryTranslation.objects.filter(master_id=entry.pk).count(), 2
        )
        self.assertContains(resp, "protected related objects")
        self.assertNotContains(resp, "Successfully deleted")

    def test_a_selection_of_only_referenced_tools_is_blocked(self):
        tools = [make_tool(f"d4c-bulk-ref-{i}") for i in range(2)]
        comparison = make_comparison("d4c-bulk-cmp-all")
        entries = [
            add_entry(comparison, tool, position=10 * (i + 1))
            for i, tool in enumerate(tools)
        ]

        resp = self.bulk_delete(tools)

        self.assertEqual(Tool.objects.filter(pk__in=[t.pk for t in tools]).count(), 2)
        self.assertEqual(
            ComparisonToolEntry.objects.filter(pk__in=[e.pk for e in entries]).count(), 2
        )
        self.assertEqual(
            ComparisonToolEntryTranslation.objects.filter(
                master_id__in=[e.pk for e in entries]
            ).count(),
            4,
        )
        self.assertContains(resp, "protected related objects")
        self.assertNotContains(resp, "Successfully deleted")


class GuardQueryContractTests(ToolDeletionGuardTestCase):
    """The protection check must be one bundled query, never one per tool."""

    def test_the_blocked_tool_lookup_is_a_single_query_for_many_tools(self):
        from django.contrib import admin as django_admin

        from catalog.admin import ToolAdmin

        tools = [make_tool(f"d4c-q-{i}") for i in range(6)]
        comparison = make_comparison("d4c-q-cmp")
        add_entry(comparison, tools[0])
        add_entry(comparison, tools[3], position=20)

        tool_admin = ToolAdmin(Tool, django_admin.site)
        with self.assertNumQueries(1):
            blocked = tool_admin.tools_blocked_by_comparison_entries(tools)

        self.assertEqual({t.pk for t in blocked}, {tools[0].pk, tools[3].pk})

    def test_an_empty_selection_runs_no_query_at_all(self):
        from django.contrib import admin as django_admin

        from catalog.admin import ToolAdmin

        tool_admin = ToolAdmin(Tool, django_admin.site)
        with self.assertNumQueries(0):
            self.assertEqual(tool_admin.tools_blocked_by_comparison_entries([]), [])

    def test_each_blocked_tool_appears_only_once(self):
        from django.contrib import admin as django_admin

        from catalog.admin import ToolAdmin

        tool = make_tool("d4c-q-multi")
        first = make_comparison("d4c-q-c1")
        second = make_comparison("d4c-q-c2")
        add_entry(first, tool)
        add_entry(second, tool)

        tool_admin = ToolAdmin(Tool, django_admin.site)
        blocked = tool_admin.tools_blocked_by_comparison_entries([tool])
        self.assertEqual([t.pk for t in blocked], [tool.pk])


class RolesAreUnchangedTests(ToolDeletionGuardTestCase):
    """D4C must not hand Author or Editor any ToolAdmin surface."""

    def test_author_and_editor_still_have_no_tool_admin_access(self):
        for group in ("Author", "Editor"):
            with self.subTest(group=group):
                user = User.objects.create_user(
                    username=f"d4c-{group.lower()}", password="pw", is_staff=True
                )
                user.groups.add(Group.objects.get(name=group))
                self.client.force_login(user)

                tool = make_tool(f"d4c-role-{group.lower()}")
                changelist = self.client.get(self.changelist_url())
                self.assertIn(changelist.status_code, (302, 403))

                delete = self.client.post(self.delete_url(tool), {"post": "yes"})
                self.assertIn(delete.status_code, (302, 403))
                self.assertTrue(Tool.objects.filter(pk=tool.pk).exists())


class DirectOrmDeleteStillCascadesTests(TestCase):
    """
    Pins the deliberate boundary: D4C protects the *admin*, not the ORM. A
    maintenance script calling ``tool.delete()`` still removes the entry and
    its translations, exactly as before this slice. Documented, not endorsed -
    the admin is the sanctioned editorial path.
    """

    def test_a_direct_delete_outside_the_admin_still_cascades(self):
        tool = make_tool("d4c-orm")
        comparison = make_comparison("d4c-orm-cmp")
        entry = add_entry(comparison, tool)

        self.assertEqual(
            ComparisonToolEntryTranslation.objects.filter(master_id=entry.pk).count(), 2
        )

        tool.delete()

        self.assertFalse(Tool.objects.filter(pk=tool.pk).exists())
        self.assertFalse(ComparisonToolEntry.objects.filter(pk=entry.pk).exists())
        self.assertEqual(
            ComparisonToolEntryTranslation.objects.filter(master_id=entry.pk).count(), 0
        )
        self.assertTrue(Comparison.objects.filter(pk=comparison.pk).exists())


class PublicSurfacesAreUnaffectedTests(TestCase):
    """D4B's fail-closed rendering and the comparison projection keep working -
    D4C adds no rendering behaviour of its own."""

    def test_the_public_comparison_projection_skips_a_deleted_tool(self):
        from compare.presentation import public_tool_entries

        tool = make_tool("d4c-pub")
        comparison = make_comparison("d4c-pub-cmp")
        add_entry(comparison, tool)

        tool.delete()

        fresh = Comparison.objects.get(pk=comparison.pk)
        self.assertEqual(public_tool_entries(fresh, "en"), [])
