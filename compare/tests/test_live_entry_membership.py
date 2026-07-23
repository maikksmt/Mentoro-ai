"""
Beta 11.9B: the entry snapshot's *membership* contract.

Beta 11.9A hardened tool resolution within an already-snapshotted entry. This
module pins the boundary that guarantee depends on: which entries are even
candidates for that resolution in the first place. ``live_entries`` is
written once, at publish time, by ``Comparison.build_live_entries()`` -
independent of the ``ComparisonToolEntry`` rows that exist afterwards.

Four draft mutations must never reach the public page before a republish,
regardless of any global tool state change in between:

* a new draft entry (never in the snapshot at all),
* a draft tool swap on an already-snapshotted entry,
* a draft deletion of an already-snapshotted entry's row,
* a draft reordering of already-snapshotted entries.

The counterpart that *is* legitimate: a tool that was already a snapshot
member, but not yet globally public at publish time, must appear the moment
that tool becomes public - with no comparison republish at all. That case is
Beta 11.9A's contract (see ``test_live_entry_tool_visibility.py``); this
module's ``AlreadySnapshottedToolBecomesPublicTests`` draws the line against
it explicitly, using a distinct tool for a distinct assertion.
"""
from datetime import timedelta

from django.conf import settings
from django.test import TestCase
from django.utils import timezone, translation

from catalog.models import Tool
from compare.models import Comparison
from compare.presentation import public_tool_entries
from compare.tests.live_snapshot_fixtures import (
    add_entry,
    make_comparison,
    make_tool,
    make_user,
    publish,
    request_rework,
    start_review_round,
)

PAST = timezone.now() - timedelta(days=1)
FUTURE = timezone.now() + timedelta(days=30)


class MembershipTestCase(TestCase):
    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.author = make_user("cmp-membership-author")

    def _html(self, slug):
        return self.client.get(f"/en/compare/{slug}/").content.decode()

    def _live_tool_ids(self, comparison):
        return [item["tool_id"] for item in comparison.live_entries]

    def _draft_tool_ids(self, comparison):
        return list(
            comparison.tool_entries.order_by("position", "pk").values_list("tool_id", flat=True)
        )


class NewDraftEntryTests(MembershipTestCase):
    """Group A: an entry created after publish is not a snapshot member,
    however its tool's global state changes."""

    def setUp(self):
        super().setUp()
        self.tool_a = make_tool("memb-new-a", "Memb New A", published_at=PAST)
        self.tool_c = make_tool("memb-new-c", "Memb New C", published_at=PAST)
        self.tool_b = make_tool("memb-new-b", "Memb New B", published_at=FUTURE)
        self.comparison = make_comparison(
            slug="memb-new-cmp", title="Memb New Scenario", author=self.author
        )
        add_entry(self.comparison, self.tool_a, position=10, summary="Summary A")
        add_entry(self.comparison, self.tool_c, position=30, summary="Summary C")
        published = publish(self.comparison, self.author)
        self.snapshot_before = published.live_entries

        reviewed = start_review_round(published, self.author)
        self.entry_b = add_entry(reviewed, self.tool_b, position=20, summary="Summary B")

    def test_snapshot_contains_only_the_originally_published_tools(self):
        comparison = Comparison.objects.get(pk=self.comparison.pk)
        self.assertEqual(
            sorted(self._live_tool_ids(comparison)),
            sorted([self.tool_a.pk, self.tool_c.pk]),
        )
        self.assertEqual(comparison.live_entries, self.snapshot_before)

    def test_draft_row_b_exists_but_is_not_a_snapshot_member(self):
        comparison = Comparison.objects.get(pk=self.comparison.pk)
        self.assertIn(self.tool_b.pk, self._draft_tool_ids(comparison))
        self.assertNotIn(self.tool_b.pk, self._live_tool_ids(comparison))

    def test_b_hidden_while_its_tool_is_not_yet_public(self):
        comparison = Comparison.objects.get(pk=self.comparison.pk)
        entries = public_tool_entries(comparison, "en")
        self.assertEqual([e.tool.pk for e in entries], [self.tool_a.pk, self.tool_c.pk])
        html = self._html("memb-new-cmp")
        self.assertNotIn("Memb New B", html)

    def test_b_stays_hidden_once_its_tool_becomes_globally_public(self):
        """The critical case: a tool becoming public must not resurrect a
        draft-only entry - only republish may do that."""
        Tool.objects.filter(pk=self.tool_b.pk).update(published_at=PAST)

        comparison = Comparison.objects.get(pk=self.comparison.pk)
        entries = public_tool_entries(comparison, "en")
        self.assertEqual([e.tool.pk for e in entries], [self.tool_a.pk, self.tool_c.pk])
        self.assertEqual(comparison.live_entries, self.snapshot_before)

        html = self._html("memb-new-cmp")
        self.assertNotIn("Memb New B", html)
        self.assertNotIn("Summary B", html)

        json_ld = self.client.get("/en/compare/memb-new-cmp/").context["seo"].json_ld
        names = [item["name"] for item in json_ld["about"]]
        self.assertNotIn("Memb New B", names)

    def test_republish_finally_activates_b(self):
        Tool.objects.filter(pk=self.tool_b.pk).update(published_at=PAST)
        in_review = Comparison.objects.get(pk=self.comparison.pk)
        republished = publish(in_review, self.author)

        self.assertEqual(
            sorted(self._live_tool_ids(republished)),
            sorted([self.tool_a.pk, self.tool_b.pk, self.tool_c.pk]),
        )
        html = self._html("memb-new-cmp")
        self.assertIn("Memb New B", html)


class ReworkNewEntryTests(MembershipTestCase):
    """Group B: identical scenario via the real rework transition."""

    def setUp(self):
        super().setUp()
        self.editor = make_user("cmp-membership-editor")
        self.tool_a = make_tool("memb-rw-a", "Memb Rework A", published_at=PAST)
        self.tool_b = make_tool("memb-rw-b", "Memb Rework B", published_at=FUTURE)
        self.comparison = make_comparison(
            slug="memb-rw-cmp", title="Memb Rework Scenario", author=self.author
        )
        add_entry(self.comparison, self.tool_a, position=10, summary="Summary A")
        published = publish(self.comparison, self.author)

        reviewed = start_review_round(published, self.author)
        add_entry(reviewed, self.tool_b, position=20, summary="Summary B")
        request_rework(reviewed, self.editor)

    def test_new_entry_hidden_through_rework(self):
        comparison = Comparison.objects.get(pk=self.comparison.pk)
        self.assertEqual(comparison.status, Comparison.STATUS_REWORK)
        self.assertNotIn(self.tool_b.pk, self._live_tool_ids(comparison))
        self.assertNotIn("Memb Rework B", self._html("memb-rw-cmp"))

    def test_global_tool_publish_does_not_activate_it_during_rework(self):
        Tool.objects.filter(pk=self.tool_b.pk).update(published_at=PAST)
        self.assertNotIn("Memb Rework B", self._html("memb-rw-cmp"))

    def test_republish_activates_it(self):
        Tool.objects.filter(pk=self.tool_b.pk).update(published_at=PAST)
        in_rework = Comparison.objects.get(pk=self.comparison.pk)
        self.assertEqual(in_rework.status, Comparison.STATUS_REWORK)
        # publish() moves REWORK -> REVIEW -> APPROVED -> PUBLISHED itself.
        republished = publish(in_rework, self.editor)
        self.assertIn(self.tool_b.pk, self._live_tool_ids(republished))
        self.assertIn("Memb Rework B", self._html("memb-rw-cmp"))


class DraftToolSwapTests(MembershipTestCase):
    """Group D: swapping an already-snapshotted entry's tool in the draft
    must not change what the public page shows before republish."""

    def setUp(self):
        super().setUp()
        self.tool_a = make_tool("memb-swap-a", "Memb Swap A", published_at=PAST)
        self.tool_b = make_tool("memb-swap-b", "Memb Swap B", published_at=PAST)
        self.comparison = make_comparison(
            slug="memb-swap-cmp", title="Memb Swap Scenario", author=self.author
        )
        self.entry = add_entry(self.comparison, self.tool_a, position=10, summary="Summary A")
        self.published = publish(self.comparison, self.author)

        reviewed = start_review_round(self.published, self.author)
        fresh_entry = reviewed.tool_entries.get(pk=self.entry.pk)
        fresh_entry.tool = self.tool_b
        fresh_entry.save(update_fields=["tool"])

    def test_snapshot_still_names_tool_a(self):
        comparison = Comparison.objects.get(pk=self.comparison.pk)
        self.assertEqual(self._live_tool_ids(comparison), [self.tool_a.pk])

    def test_draft_row_already_points_at_tool_b(self):
        comparison = Comparison.objects.get(pk=self.comparison.pk)
        self.assertEqual(self._draft_tool_ids(comparison), [self.tool_b.pk])

    def test_public_page_still_shows_tool_a_not_b(self):
        html = self._html("memb-swap-cmp")
        self.assertIn("Memb Swap A", html)
        self.assertNotIn("Memb Swap B", html)

    def test_republish_activates_the_swap(self):
        in_review = Comparison.objects.get(pk=self.comparison.pk)
        republished = publish(in_review, self.author)
        self.assertEqual(self._live_tool_ids(republished), [self.tool_b.pk])
        html = self._html("memb-swap-cmp")
        self.assertNotIn("Memb Swap A", html)
        self.assertIn("Memb Swap B", html)


class DraftDeletionTests(MembershipTestCase):
    """Group E: deleting an already-snapshotted entry's draft row must not
    remove it from the public page before republish."""

    def setUp(self):
        super().setUp()
        self.tool_a = make_tool("memb-del-a", "Memb Del A", published_at=PAST)
        self.comparison = make_comparison(
            slug="memb-del-cmp", title="Memb Del Scenario", author=self.author
        )
        self.entry = add_entry(self.comparison, self.tool_a, position=10, summary="Summary A")
        self.published = publish(self.comparison, self.author)

        reviewed = start_review_round(self.published, self.author)
        reviewed.tool_entries.get(pk=self.entry.pk).delete()

    def test_snapshot_still_contains_a(self):
        comparison = Comparison.objects.get(pk=self.comparison.pk)
        self.assertEqual(self._live_tool_ids(comparison), [self.tool_a.pk])

    def test_draft_row_is_gone(self):
        comparison = Comparison.objects.get(pk=self.comparison.pk)
        self.assertEqual(comparison.tool_entries.count(), 0)

    def test_public_page_still_shows_a(self):
        html = self._html("memb-del-cmp")
        self.assertIn("Memb Del A", html)
        self.assertIn("Summary A", html)

    def test_republish_removes_it(self):
        in_review = Comparison.objects.get(pk=self.comparison.pk)
        republished = publish(in_review, self.author)
        self.assertEqual(republished.live_entries, [])
        html = self._html("memb-del-cmp")
        self.assertNotIn("Memb Del A", html)


class DraftReorderTests(MembershipTestCase):
    """Group F: reordering draft positions must not reorder the public
    page before republish."""

    def setUp(self):
        super().setUp()
        self.tool_a = make_tool("memb-order-a", "Memb Order A", published_at=PAST)
        self.tool_b = make_tool("memb-order-b", "Memb Order B", published_at=PAST)
        self.tool_c = make_tool("memb-order-c", "Memb Order C", published_at=PAST)
        self.comparison = make_comparison(
            slug="memb-order-cmp", title="Memb Order Scenario", author=self.author
        )
        self.entry_a = add_entry(self.comparison, self.tool_a, position=10, summary="Summary A")
        self.entry_b = add_entry(self.comparison, self.tool_b, position=20, summary="Summary B")
        self.entry_c = add_entry(self.comparison, self.tool_c, position=30, summary="Summary C")
        self.published = publish(self.comparison, self.author)

        reviewed = start_review_round(self.published, self.author)
        reviewed.tool_entries.filter(pk=self.entry_a.pk).update(position=99)
        reviewed.tool_entries.filter(pk=self.entry_c.pk).update(position=5)

    def _rendered_order(self, html):
        positions = {
            name: html.find(name)
            for name in ("Memb Order A", "Memb Order B", "Memb Order C")
        }
        return [name for name, _pos in sorted(positions.items(), key=lambda kv: kv[1])]

    def test_snapshot_order_is_unchanged(self):
        comparison = Comparison.objects.get(pk=self.comparison.pk)
        self.assertEqual(
            self._live_tool_ids(comparison),
            [self.tool_a.pk, self.tool_b.pk, self.tool_c.pk],
        )

    def test_draft_order_is_already_reversed(self):
        comparison = Comparison.objects.get(pk=self.comparison.pk)
        self.assertEqual(
            self._draft_tool_ids(comparison),
            [self.tool_c.pk, self.tool_b.pk, self.tool_a.pk],
        )

    def test_public_page_keeps_the_published_order(self):
        html = self._html("memb-order-cmp")
        self.assertEqual(
            self._rendered_order(html), ["Memb Order A", "Memb Order B", "Memb Order C"]
        )

    def test_republish_activates_the_new_order(self):
        in_review = Comparison.objects.get(pk=self.comparison.pk)
        republished = publish(in_review, self.author)
        self.assertEqual(
            self._live_tool_ids(republished),
            [self.tool_c.pk, self.tool_b.pk, self.tool_a.pk],
        )
        html = self._html("memb-order-cmp")
        self.assertEqual(
            self._rendered_order(html), ["Memb Order C", "Memb Order B", "Memb Order A"]
        )


class AlreadySnapshottedToolBecomesPublicTests(MembershipTestCase):
    """Group G: the legitimate counterpart. A tool that WAS a snapshot
    member at publish time, but not yet globally public, activates on its
    own once it becomes public - no republish needed. This is what
    distinguishes the global tool contract (Beta 11.9A) from the draft
    membership contract (this module)."""

    def setUp(self):
        super().setUp()
        self.tool_a = make_tool("memb-legit-a", "Memb Legit A", published_at=PAST)
        self.tool_b = make_tool("memb-legit-b", "Memb Legit B", published_at=FUTURE)
        self.comparison = make_comparison(
            slug="memb-legit-cmp", title="Memb Legit Scenario", author=self.author
        )
        add_entry(self.comparison, self.tool_a, position=10, summary="Summary A")
        add_entry(self.comparison, self.tool_b, position=20, summary="Summary B")
        self.published = publish(self.comparison, self.author)

    def test_b_is_a_snapshot_member_from_the_start(self):
        self.assertIn(self.tool_b.pk, self._live_tool_ids(self.published))

    def test_b_is_hidden_while_not_yet_public(self):
        html = self._html("memb-legit-cmp")
        self.assertNotIn("Memb Legit B", html)

    def test_b_appears_once_public_with_no_republish(self):
        snapshot_before = Comparison.objects.get(pk=self.comparison.pk).live_entries
        Tool.objects.filter(pk=self.tool_b.pk).update(published_at=PAST)

        html = self._html("memb-legit-cmp")
        self.assertIn("Memb Legit B", html)
        snapshot_after = Comparison.objects.get(pk=self.comparison.pk).live_entries
        self.assertEqual(snapshot_before, snapshot_after)


class DataIntegrityTests(MembershipTestCase):
    """Group K: reading the projection through a public GET mutates nothing."""

    def setUp(self):
        super().setUp()
        self.tool_a = make_tool("memb-integrity-a", "Memb Integrity A", published_at=PAST)
        self.comparison = make_comparison(
            slug="memb-integrity-cmp", title="Memb Integrity Scenario", author=self.author
        )
        add_entry(self.comparison, self.tool_a, position=10, summary="Summary A")
        self.published = publish(self.comparison, self.author)
        reviewed = start_review_round(self.published, self.author)
        add_entry(reviewed, make_tool("memb-integrity-b", "Memb Integrity B", published_at=FUTURE),
                   position=20, summary="Summary B")

    def test_repeated_public_gets_do_not_mutate_state(self):
        before = Comparison.objects.get(pk=self.comparison.pk)
        before_snapshot = before.live_entries
        before_i18n = before.live_i18n
        before_status = before.status
        before_updated = before.updated_at
        before_draft_rows = list(
            before.tool_entries.values_list("pk", "position", "tool_id")
        )
        before_revision_count = before.tool_entries.count()

        for _ in range(3):
            self._html("memb-integrity-cmp")

        after = Comparison.objects.get(pk=self.comparison.pk)
        self.assertEqual(after.live_entries, before_snapshot)
        self.assertEqual(after.live_i18n, before_i18n)
        self.assertEqual(after.status, before_status)
        self.assertEqual(after.updated_at, before_updated)
        self.assertEqual(
            list(after.tool_entries.values_list("pk", "position", "tool_id")),
            before_draft_rows,
        )
        self.assertEqual(after.tool_entries.count(), before_revision_count)
