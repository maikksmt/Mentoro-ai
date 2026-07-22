"""
Beta 11.7 groups A/G: which use cases are publicly visible at all, and the
guarantee that reading a public page changes nothing.

The defect this closes: editing a published use case moved it to review
(the admin's own auto-review guard) and took its entire public page offline,
even though the published ``live_i18n`` snapshot was still intact. Guide and
Prompt never had that problem because their querysets already used
``visible_on_site()``; ``UseCaseQuerySet`` used the stricter ``published()``.
"""
from django.conf import settings
from django.test import TestCase
from django.utils import translation
from reversion.models import Version

from core.models.editorial import EditorialWorkflowMixin
from usecases.models import UseCase
from usecases.tests.live_visibility_fixtures import (
    DRAFT_MARKER,
    LIVE_MARKER,
    archive,
    make_usecase,
    make_user,
    publish,
    save_draft_edit,
    start_review_round,
)


class LiveVisibilityBaseStateTests(TestCase):
    """Group A: the status/snapshot matrix, through the real queryset."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.author = make_user("uc-vis-author")

    def _visible(self, usecase, language="en"):
        return UseCase.objects.visible_in_language(language).filter(pk=usecase.pk).exists()

    def test_never_published_draft_is_invisible(self):
        usecase = make_usecase(slug="vis-never-published", title="Never Published", author=self.author)
        self.assertFalse(self._visible(usecase))

    def test_published_usecase_is_visible(self):
        usecase = make_usecase(slug="vis-published", title="Published", author=self.author)
        published = publish(usecase, self.author)
        self.assertEqual(published.status, EditorialWorkflowMixin.STATUS_PUBLISHED)
        self.assertTrue(self._visible(published))

    def test_published_then_edited_usecase_stays_visible(self):
        """The core Beta 11.7 contract."""
        usecase = make_usecase(slug="vis-edited", title=LIVE_MARKER, author=self.author)
        publish(usecase, self.author)
        save_draft_edit(usecase, "en", title=DRAFT_MARKER)
        in_review = start_review_round(usecase, self.author)

        self.assertEqual(in_review.status, EditorialWorkflowMixin.STATUS_REVIEW)
        self.assertTrue(self._visible(in_review))

    def test_review_status_with_live_snapshot_is_visible(self):
        usecase = make_usecase(slug="vis-review", title="Review With Live", author=self.author)
        publish(usecase, self.author)
        in_review = start_review_round(usecase, self.author)
        self.assertTrue(self._visible(in_review))

    def test_review_status_without_a_live_revision_stays_invisible(self):
        """Fail-closed: a never-published draft sent to review has no live
        revision marker and must not become public."""
        usecase = make_usecase(slug="vis-review-no-live", title="Review No Live", author=self.author)
        usecase.move_to_review(by=self.author)
        usecase.save()
        self.assertFalse(self._visible(usecase))

    def test_approved_status_with_live_snapshot_is_visible(self):
        usecase = make_usecase(slug="vis-approved", title="Approved With Live", author=self.author)
        publish(usecase, self.author)
        in_review = start_review_round(usecase, self.author)
        in_review.approve(by=self.author)
        in_review.save()
        approved = UseCase.objects.get(pk=in_review.pk)
        self.assertEqual(approved.status, EditorialWorkflowMixin.STATUS_APPROVED)
        self.assertTrue(self._visible(approved))

    def test_archived_usecase_is_invisible_despite_a_live_snapshot(self):
        """An explicit withdrawal must win over an intact old snapshot."""
        usecase = make_usecase(slug="vis-archived", title="Archived", author=self.author)
        published = publish(usecase, self.author)
        self.assertTrue(self._visible(published))

        archived = archive(published, self.author)
        self.assertEqual(archived.status, EditorialWorkflowMixin.STATUS_ARCHIVED)
        self.assertTrue(archived.live_i18n, "snapshot deliberately left intact")
        self.assertFalse(self._visible(archived))

    def test_live_snapshot_in_another_language_does_not_make_it_visible(self):
        usecase = make_usecase(slug="vis-en-only", title="EN Only", author=self.author)
        published = publish(usecase, self.author)
        self.assertTrue(self._visible(published, "en"))
        self.assertFalse(self._visible(published, "de"))

    def test_republished_usecase_is_visible_again(self):
        usecase = make_usecase(slug="vis-republish", title=LIVE_MARKER, author=self.author)
        publish(usecase, self.author)
        save_draft_edit(usecase, "en", title=DRAFT_MARKER)
        in_review = start_review_round(usecase, self.author)
        republished = publish(in_review, self.author)

        self.assertEqual(republished.status, EditorialWorkflowMixin.STATUS_PUBLISHED)
        self.assertTrue(self._visible(republished))
        self.assertEqual(republished.live_i18n["en"]["title"], DRAFT_MARKER)


class PublicReadIsSideEffectFreeTests(TestCase):
    """Group G: a public GET changes nothing at all."""

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)
        self.author = make_user("uc-integrity-author")
        self.usecase = make_usecase(
            slug="integrity-uc", title=LIVE_MARKER, author=self.author, persona="Live Persona"
        )
        publish(self.usecase, self.author)
        save_draft_edit(self.usecase, "en", title=DRAFT_MARKER, persona="Draft Persona")
        start_review_round(self.usecase, self.author)

    def _state(self):
        usecase = UseCase.objects.get(pk=self.usecase.pk)
        return {
            "status": usecase.status,
            "live_i18n": usecase.live_i18n,
            "last_published_revision_id": usecase.last_published_revision_id,
            "reviewed_at": usecase.reviewed_at,
            "reviewed_by_id": usecase.reviewed_by_id,
            "published_at": usecase.published_at,
            "updated_at": usecase.updated_at,
            "title": usecase.safe_translation_getter("title", language_code="en"),
            "persona": usecase.safe_translation_getter("persona", language_code="en"),
            "tools": list(usecase.tools.values_list("pk", flat=True)),
            "revision_count": Version.objects.get_for_object(usecase).count(),
        }

    def test_detail_get_changes_no_persisted_state(self):
        before = self._state()
        resp = self.client.get("/en/usecases/integrity-uc/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(before, self._state())

    def test_list_get_changes_no_persisted_state(self):
        before = self._state()
        resp = self.client.get("/en/usecases/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(before, self._state())

    def test_repeated_public_gets_change_no_persisted_state(self):
        before = self._state()
        for _ in range(3):
            self.client.get("/en/usecases/integrity-uc/")
            self.client.get("/en/usecases/")
        self.assertEqual(before, self._state())

    def test_public_gets_create_no_revision(self):
        before = Version.objects.count()
        self.client.get("/en/usecases/integrity-uc/")
        self.client.get("/en/usecases/")
        self.assertEqual(Version.objects.count(), before)
