"""
Beta 8.10a Section A: reproduction of the confirmed public draft-slug leak.

Workflow reproduced:
1. Publish a guide (old slug, old title/intro/body).
2. Record the old live slug.
3. Start a new revision: change title/intro/body AND slug.
4. Move status to review (matching the real editorial workflow;
   last_published_revision_id set, matching visible_on_site()'s existing
   review/approved-with-live-revision branch).
5. Request the old slug publicly.
6. Request the new slug publicly.

Confirmed behavior BEFORE the fix (Beta 8.10): the new, unpublished slug
resolved successfully (HTTP 200) because GuideDetailView's slug resolution
checked the current translation's slug field FIRST, only falling back to
the live_i18n snapshot if no direct match was found - but the current
translation's slug field is updated the instant an editor edits it,
independent of publish status.

Required behavior AFTER the fix:
    old live slug     -> HTTP 200, old (live) content
    new unpublished slug -> HTTP 404
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models.editorial import EditorialWorkflowMixin
from guides.models import Guide

User = get_user_model()


class DraftSlugLeakReproductionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user(
            username="draft-slug-editor", email="draft-slug@example.com", password="testpass123"
        )

    def test_old_slug_200_new_draft_slug_404(self):
        # 1 + 2: publish, record old slug.
        g = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_APPROVED, author=self.author)
        g.create_translation("en", title="Old Public Title", intro="Old public intro",
                              body="Old public body", slug="old-live-slug-810a")
        g.publish(by=self.author)
        g.save()
        old_slug = g.public_slug or g.slug
        self.assertEqual(old_slug, "old-live-slug-810a")

        # 3: new revision - title, intro, body AND slug all change.
        g.title = "New Draft Title"
        g.intro = "New draft intro"
        g.body = "New draft body"
        g.slug = "new-draft-slug-810a"
        g.save()

        # 4: real workflow transition to review, with a live revision on record.
        g.move_to_review(by=self.author)
        g.last_published_revision_id = 1
        g.save()

        # 5: old slug must still resolve, with the OLD (live) content.
        resp_old = self.client.get(f"/en/guides/{old_slug}/")
        self.assertEqual(resp_old.status_code, 200)
        html_old = resp_old.content.decode()
        self.assertIn("Old Public Title", html_old)
        self.assertIn("Old public body", html_old)
        self.assertNotIn("New Draft Title", html_old)
        self.assertNotIn("New draft body", html_old)

        # 6: the new, unpublished slug must NOT resolve at all.
        resp_new = self.client.get("/en/guides/new-draft-slug-810a/")
        self.assertEqual(resp_new.status_code, 404)

    def test_new_draft_slug_absent_from_list_page_links(self):
        g = Guide.objects.create(status=EditorialWorkflowMixin.STATUS_APPROVED, author=self.author)
        g.create_translation("en", title="List Old Title", intro="i", body="b", slug="list-old-slug-810a")
        g.publish(by=self.author)
        g.save()

        g.title = "List New Draft Title"
        g.slug = "list-new-draft-slug-810a"
        g.save()
        g.move_to_review(by=self.author)
        g.last_published_revision_id = 1
        g.save()

        resp = self.client.get("/en/guides/")
        html = resp.content.decode()
        self.assertIn("list-old-slug-810a", html)
        self.assertNotIn("list-new-draft-slug-810a", html)
        self.assertIn("List Old Title", html)
        self.assertNotIn("List New Draft Title", html)
