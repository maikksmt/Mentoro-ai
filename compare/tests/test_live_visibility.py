"""
Beta 11.9 groups A/B: which comparisons are publicly visible, and the
guarantee that only published parent values are rendered.

The defect this closes: editing a published comparison moved it to review
(the admin's own auto-review guard) and took its entire public page offline,
even though the published ``live_i18n`` snapshot was intact. Beta 11.1
reproduced it and deliberately deferred the fix, because widening visibility
without an entry snapshot would have published unreviewed tool-entry edits -
see compare/tests/test_live_entries.py for the entry half of the contract.
"""
from django.conf import settings
from django.test import TestCase
from django.utils import translation

from compare.models import Comparison
from compare.tests.live_snapshot_fixtures import (
    add_entry,
    archive,
    make_comparison,
    make_legacy_published,
    make_tool,
    make_user,
    publish,
    request_rework,
    save_draft_edit,
    start_review_round,
)
from core.models.editorial import EditorialWorkflowMixin


class WorkflowVisibilityTests(TestCase):
    """Group A: the status/snapshot matrix, through the real queryset."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.author = make_user("cmp-vis-author")
        self.editor = make_user("cmp-vis-editor")

    def _visible(self, comparison, language="en"):
        return (
            Comparison.objects.visible_in_language(language)
            .filter(pk=comparison.pk)
            .exists()
        )

    def test_never_published_draft_is_invisible(self):
        comparison = make_comparison(
            slug="vis-never-published", title="Never Published", author=self.author
        )
        self.assertFalse(self._visible(comparison))

    def test_published_comparison_is_visible(self):
        comparison = make_comparison(slug="vis-published", title="Published", author=self.author)
        published = publish(comparison, self.author)
        self.assertEqual(published.status, EditorialWorkflowMixin.STATUS_PUBLISHED)
        self.assertTrue(self._visible(published))

    def test_publishing_writes_an_entry_snapshot(self):
        comparison = make_comparison(slug="vis-snapshot", title="Snapshot", author=self.author)
        tool = make_tool("vis-snapshot-tool", "Snapshot Tool")
        add_entry(comparison, tool, position=10, summary="Published summary")
        published = publish(comparison, self.author)

        self.assertIsNotNone(published.live_entries)
        self.assertEqual(len(published.live_entries), 1)
        self.assertEqual(published.live_entries[0]["tool_id"], tool.pk)

    def test_published_then_edited_comparison_stays_visible(self):
        """The core Beta 11.9 contract."""
        comparison = make_comparison(slug="vis-edited", title="Live Title", author=self.author)
        publish(comparison, self.author)
        save_draft_edit(comparison, "en", title="Draft Title")
        in_review = start_review_round(comparison, self.author)

        self.assertEqual(in_review.status, EditorialWorkflowMixin.STATUS_REVIEW)
        self.assertTrue(self._visible(in_review))

    def test_approved_status_stays_visible(self):
        comparison = make_comparison(slug="vis-approved", title="Approved", author=self.author)
        publish(comparison, self.author)
        in_review = start_review_round(comparison, self.author)
        in_review.approve(by=self.editor)
        in_review.save()
        approved = Comparison.objects.get(pk=in_review.pk)

        self.assertEqual(approved.status, EditorialWorkflowMixin.STATUS_APPROVED)
        self.assertTrue(self._visible(approved))

    def test_rework_status_stays_visible(self):
        comparison = make_comparison(slug="vis-rework", title="Rework", author=self.author)
        publish(comparison, self.author)
        start_review_round(comparison, self.author)
        reworked = request_rework(comparison, self.editor)

        self.assertEqual(reworked.status, EditorialWorkflowMixin.STATUS_REWORK)
        self.assertTrue(self._visible(reworked))

    def test_review_without_a_live_revision_stays_invisible(self):
        comparison = make_comparison(
            slug="vis-review-no-live", title="Review No Live", author=self.author
        )
        comparison.move_to_review(by=self.author)
        comparison.save()
        self.assertFalse(self._visible(comparison))

    def test_archived_comparison_is_invisible_despite_snapshots(self):
        comparison = make_comparison(slug="vis-archived", title="Archived", author=self.author)
        published = publish(comparison, self.author)
        self.assertTrue(self._visible(published))

        archived = archive(published, self.editor)
        self.assertEqual(archived.status, EditorialWorkflowMixin.STATUS_ARCHIVED)
        self.assertTrue(archived.live_i18n, "parent snapshot deliberately left intact")
        self.assertIsNotNone(archived.live_entries, "entry snapshot deliberately left intact")
        self.assertFalse(self._visible(archived))

    def test_live_snapshot_in_another_language_does_not_make_it_visible(self):
        comparison = make_comparison(slug="vis-en-only", title="EN Only", author=self.author)
        published = publish(comparison, self.author)
        self.assertTrue(self._visible(published, "en"))
        self.assertFalse(self._visible(published, "de"))

    def test_republished_comparison_is_visible_again(self):
        comparison = make_comparison(slug="vis-republish", title="Live Title", author=self.author)
        publish(comparison, self.author)
        save_draft_edit(comparison, "en", title="Draft Title")
        in_review = start_review_round(comparison, self.author)
        republished = publish(in_review, self.author)

        self.assertEqual(republished.status, EditorialWorkflowMixin.STATUS_PUBLISHED)
        self.assertTrue(self._visible(republished))
        self.assertEqual(republished.live_i18n["en"]["title"], "Draft Title")


class LegacyRecordVisibilityTests(TestCase):
    """
    Records published before Beta 11.9 carry no entry snapshot
    (``live_entries IS NULL``). They keep exactly their pre-11.9 behaviour:
    public while ``published``, offline the moment an edit moves them out of
    it - because their entries would otherwise have to be read from the live
    rows while unreviewed.
    """

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.author = make_user("cmp-legacy-author")

    def _visible(self, comparison, language="en"):
        return (
            Comparison.objects.visible_in_language(language)
            .filter(pk=comparison.pk)
            .exists()
        )

    def test_legacy_published_record_stays_visible(self):
        comparison = make_comparison(
            slug="legacy-published", title="Legacy Published", author=self.author
        )
        legacy = make_legacy_published(comparison, self.author)

        self.assertIsNone(legacy.live_entries)
        self.assertEqual(legacy.status, EditorialWorkflowMixin.STATUS_PUBLISHED)
        self.assertTrue(self._visible(legacy))

    def test_legacy_record_in_review_stays_offline(self):
        """Fail-closed: without an entry snapshot there is no provable
        published entry state, so the widened branch must not admit it."""
        comparison = make_comparison(
            slug="legacy-review", title="Legacy Review", author=self.author
        )
        legacy = make_legacy_published(comparison, self.author)
        in_review = start_review_round(legacy, self.author)
        # start_review_round re-reads, so re-assert the legacy shape.
        Comparison.objects.filter(pk=in_review.pk).update(live_entries=None)
        in_review = Comparison.objects.get(pk=in_review.pk)

        self.assertIsNone(in_review.live_entries)
        self.assertIsNotNone(in_review.last_published_revision_id)
        self.assertEqual(in_review.status, EditorialWorkflowMixin.STATUS_REVIEW)
        self.assertFalse(self._visible(in_review))

    def test_legacy_record_gains_protection_on_next_publish(self):
        comparison = make_comparison(
            slug="legacy-republish", title="Legacy Republish", author=self.author
        )
        legacy = make_legacy_published(comparison, self.author)
        self.assertIsNone(legacy.live_entries)

        in_review = start_review_round(legacy, self.author)
        republished = publish(in_review, self.author)

        self.assertIsNotNone(republished.live_entries)
        # And now the widened branch does admit it.
        again_in_review = start_review_round(republished, self.author)
        self.assertTrue(self._visible(again_in_review))


class ParentDraftVersusLiveTests(TestCase):
    """Group B: only the last published parent values are ever rendered."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.author = make_user("cmp-parent-author")
        self.comparison = make_comparison(
            slug="parent-live-slug",
            title="Live Title A",
            intro="<p>Live intro A</p>",
            body="<p>Live body A</p>",
            author=self.author,
        )
        publish(self.comparison, self.author)
        save_draft_edit(
            self.comparison,
            "en",
            title="Draft Title B",
            intro="<p>Draft intro B</p>",
            body="<p>Draft body B</p>",
            slug="parent-draft-slug",
        )
        start_review_round(self.comparison, self.author)

    def _detail(self):
        return self.client.get("/en/compare/parent-live-slug/")

    def test_live_slug_still_resolves_during_review(self):
        self.assertEqual(self._detail().status_code, 200)

    def test_draft_slug_does_not_resolve_before_republish(self):
        self.assertEqual(self.client.get("/en/compare/parent-draft-slug/").status_code, 404)

    def test_detail_page_renders_published_title_intro_body(self):
        html = self._detail().content.decode()
        self.assertIn("Live Title A", html)
        self.assertIn("Live intro A", html)
        self.assertIn("Live body A", html)

    def test_detail_page_renders_no_draft_parent_value(self):
        html = self._detail().content.decode()
        for value in ("Draft Title B", "Draft intro B", "Draft body B", "parent-draft-slug"):
            with self.subTest(value=value):
                self.assertNotIn(value, html)

    def test_seo_and_breadcrumbs_use_published_values(self):
        resp = self._detail()
        seo = resp.context["seo"]
        self.assertEqual(seo.title, "Live Title A")
        self.assertIn("Live intro", seo.description)
        self.assertIn("parent-live-slug", seo.canonical)
        self.assertNotIn("parent-draft-slug", seo.canonical)

        labels = [str(label) for label, _url in resp.context["crumbs"]]
        self.assertIn("Live Title A", labels)
        self.assertNotIn("Draft Title B", labels)

    def test_json_ld_uses_published_values(self):
        json_ld = self._detail().context["seo"].json_ld
        self.assertEqual(json_ld["name"], "Live Title A")
        self.assertIn("parent-live-slug", json_ld["url"])

    def test_list_page_shows_published_title_only(self):
        html = self.client.get("/en/compare/").content.decode()
        self.assertIn("Live Title A", html)
        self.assertNotIn("Draft Title B", html)

    def test_republish_activates_the_new_parent_values(self):
        in_review = Comparison.objects.get(pk=self.comparison.pk)
        publish(in_review, self.author)

        resp = self.client.get("/en/compare/parent-draft-slug/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Draft Title B", resp.content.decode())
