"""
Beta 8.10 Section E: Editorial live-revisions must not be damaged by the
new strict Guide.objects.visible_in_language() resolution.

A guide that has been published keeps a live_i18n snapshot of its last
published title/intro/body/slug/public_slug (populated by
EditorialWorkflowMixin._update_live_snapshot(), called from the publish()
transition). Guide.display_title/display_intro/display_body (and
get_absolute_url()) read that snapshot first, falling back to the current
(possibly draft-in-progress) translation only if no snapshot value exists.
This is what lets a guide under active revision (status review/approved
with last_published_revision_id set - the visible_on_site() branch) keep
showing its last published content publicly while an editor works on the
next revision.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models.editorial import EditorialWorkflowMixin
from guides.models import Guide

User = get_user_model()


class GuideLiveRevisionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user(
            username="live-rev-editor", email="editor@example.com", password="testpass123"
        )

    def _publish(self, *, slug, title, intro, body):
        g = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_APPROVED, author=self.author)
        g.create_translation("en", title=title, intro=intro, body=body, slug=slug)
        g.publish(by=self.author)
        g.save()
        return g

    def test_published_guide_is_publicly_reachable(self):
        g = self._publish(slug="live-rev-1", title="Old Title", intro="Old Intro", body="Old Body")
        resp = self.client.get(f"/en/guides/{g.public_slug or g.slug}/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Old Title", resp.content.decode())

    def test_new_revision_keeps_old_title_and_body_public(self):
        g = self._publish(slug="live-rev-2", title="Old Title", intro="Old Intro", body="Old Body")
        live_slug = g.public_slug or g.slug

        # Editor begins a new, unpublished revision (title/body only, slug
        # unchanged) and moves it to review.
        g.title = "New Unpublished Title"
        g.intro = "New Unpublished Intro"
        g.body = "New Unpublished Body"
        g.save()
        g.move_to_review(by=self.author)
        g.last_published_revision_id = 1  # simulates reversion pointing at the live version
        g.save()

        resp = self.client.get(f"/en/guides/{live_slug}/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("Old Title", html)
        self.assertIn("Old Body", html)
        self.assertNotIn("New Unpublished Title", html)
        self.assertNotIn("New Unpublished Body", html)

    def test_new_slug_returns_404_not_the_unpublished_title(self):
        # Beta 8.10a: _resolve_guide_by_slug() treats the live_i18n snapshot
        # as the sole public slug once it exists for a language - the new
        # draft slug must not resolve at all, even though the underlying
        # translation row now literally carries it.
        g = self._publish(slug="live-rev-3", title="Old Title", intro="Old Intro", body="Old Body")
        live_slug = g.public_slug or g.slug

        g.title = "New Unpublished Title 2"
        g.intro = "New Unpublished Intro 2"
        g.body = "New Unpublished Body 2"
        g.slug = "live-rev-3-v2"
        g.save()
        g.move_to_review(by=self.author)
        g.last_published_revision_id = 1
        g.save()

        # The old, live URL keeps working with the old content.
        resp_old = self.client.get(f"/en/guides/{live_slug}/")
        self.assertEqual(resp_old.status_code, 200)
        self.assertIn("Old Title", resp_old.content.decode())

        # The new, unpublished slug must 404 - not resolve with old
        # content, and never with the new, unpublished title/body.
        resp_new = self.client.get("/en/guides/live-rev-3-v2/")
        self.assertEqual(resp_new.status_code, 404)

    def test_review_status_with_live_revision_stays_publicly_visible(self):
        g = self._publish(slug="live-rev-4", title="Old Title", intro="Old Intro", body="Old Body")
        live_slug = g.public_slug or g.slug
        g.move_to_review(by=self.author)
        g.last_published_revision_id = 1
        g.save()

        resp = self.client.get(f"/en/guides/{live_slug}/")
        self.assertEqual(resp.status_code, 200)

    def test_approved_status_with_live_revision_stays_publicly_visible(self):
        g = self._publish(slug="live-rev-5", title="Old Title", intro="Old Intro", body="Old Body")
        live_slug = g.public_slug or g.slug
        g.move_to_review(by=self.author)
        g.approve(by=self.author)
        g.last_published_revision_id = 1
        g.save()

        resp = self.client.get(f"/en/guides/{live_slug}/")
        self.assertEqual(resp.status_code, 200)

    def test_review_without_ever_being_published_stays_invisible(self):
        g = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_DRAFT, author=self.author)
        g.create_translation("en", title="Never Published", intro="i", body="b", slug="never-published-review")
        g.move_to_review(by=self.author)
        g.save()
        # No last_published_revision_id set - a fully-fresh review draft
        # that has never had a live version must stay unreachable.
        resp = self.client.get("/en/guides/never-published-review/")
        self.assertEqual(resp.status_code, 404)

    def test_list_still_shows_the_live_revision_guide(self):
        g = self._publish(slug="live-rev-list", title="Old Title", intro="Old Intro", body="Old Body")
        g.title = "New Unpublished Title 3"
        g.save()
        g.move_to_review(by=self.author)
        g.last_published_revision_id = 1
        g.save()

        resp = self.client.get("/en/guides/")
        objs = list(resp.context["object_list"])
        self.assertIn(g.pk, [o.pk for o in objs])
