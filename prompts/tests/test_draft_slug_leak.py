"""
Beta 8.11 Section A: reproduction of the confirmed public draft-slug leak
in PromptDetailView, structurally identical to the Guide leak fixed in
Beta 8.10a (guides/tests/test_draft_slug_leak.py).

Workflow reproduced:
1. Publish a prompt (old slug, old title/intro/body).
2. Record the old live slug.
3. Start a new revision: change title/intro/body AND slug.
4. Move status to review (matching the real editorial workflow;
   last_published_revision_id set, matching visible_on_site()'s existing
   review/approved-with-live-revision branch).
5. Request the old slug publicly.
6. Request the new slug publicly.

Confirmed behavior BEFORE the fix (Beta 8.10a and earlier):
prompts/views.py::_resolve_by_slug() checked the current translation's
slug/public_slug field FIRST (via a direct translations__slug/public_slug
filter), only falling back to the live_i18n snapshot in a per-object Python
loop if no direct match was found - but the current translation's slug
field is updated the instant an editor edits it, independent of publish
status, so the new, unpublished slug resolved successfully (HTTP 200).

Required behavior AFTER the fix:
    old live slug         -> HTTP 200, old (live) content
    new unpublished slug  -> HTTP 404
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models.editorial import EditorialWorkflowMixin
from prompts.models import Prompt

User = get_user_model()


class PromptDraftSlugLeakReproductionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user(
            username="prompt-draft-slug-editor", email="prompt-draft-slug@example.com", password="testpass123"
        )

    def test_old_slug_200_new_draft_slug_404(self):
        p = Prompt.objects.create(status=EditorialWorkflowMixin.STATUS_APPROVED, author=self.author)
        p.create_translation("en", title="Old Public Title", intro="Old public intro",
                              body="Old public body", outro="Old public outro", slug="old-live-slug-811")
        p.publish(by=self.author)
        p.save()
        old_slug = p.public_slug or p.slug
        self.assertEqual(old_slug, "old-live-slug-811")

        p.title = "New Draft Title"
        p.intro = "New draft intro"
        p.body = "New draft body"
        p.slug = "new-draft-slug-811"
        p.save()

        p.move_to_review(by=self.author)
        p.last_published_revision_id = 1
        p.save()

        resp_old = self.client.get(f"/en/prompts/{old_slug}/")
        self.assertEqual(resp_old.status_code, 200)
        html_old = resp_old.content.decode()
        self.assertIn("Old Public Title", html_old)
        self.assertIn("Old public body", html_old)
        self.assertNotIn("New Draft Title", html_old)
        self.assertNotIn("New draft body", html_old)

        resp_new = self.client.get("/en/prompts/new-draft-slug-811/")
        self.assertEqual(resp_new.status_code, 404)

    def test_canonical_and_hreflang_use_live_slug_not_draft_slug(self):
        p = Prompt.objects.create(status=EditorialWorkflowMixin.STATUS_APPROVED, author=self.author)
        p.create_translation("en", title="SEO Old Title", intro="i", body="b", outro="o",
                              slug="seo-old-slug-811")
        p.publish(by=self.author)
        p.save()
        old_slug = p.public_slug or p.slug

        p.slug = "seo-new-draft-slug-811"
        p.save()
        p.move_to_review(by=self.author)
        p.last_published_revision_id = 1
        p.save()

        resp = self.client.get(f"/en/prompts/{old_slug}/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn(old_slug, html)
        self.assertNotIn("seo-new-draft-slug-811", html)

    def test_new_draft_slug_absent_from_list_and_related(self):
        p = Prompt.objects.create(status=EditorialWorkflowMixin.STATUS_APPROVED, author=self.author)
        p.create_translation("en", title="List Old Title", intro="i", body="b", outro="o",
                              slug="list-old-slug-811")
        p.publish(by=self.author)
        p.save()

        p.title = "List New Draft Title"
        p.slug = "list-new-draft-slug-811"
        p.save()
        p.move_to_review(by=self.author)
        p.last_published_revision_id = 1
        p.save()

        resp = self.client.get("/en/prompts/")
        html = resp.content.decode()
        self.assertIn("list-old-slug-811", html)
        self.assertNotIn("list-new-draft-slug-811", html)
        self.assertIn("List Old Title", html)
        self.assertNotIn("List New Draft Title", html)

    def test_historical_published_prompt_without_live_snapshot_still_resolves(self):
        """Compat fallback: a published() prompt predating live_i18n (e.g.
        created directly, bypassing publish()) must remain reachable via its
        current translation slug."""
        p = Prompt.objects.create(status=EditorialWorkflowMixin.STATUS_PUBLISHED)
        from django.utils import timezone
        p.published_at = timezone.now()
        p.save()
        p.create_translation("en", title="Historical Title", intro="i", body="b", outro="o",
                              slug="historical-slug-811")
        self.assertEqual(p.live_i18n, {})

        resp = self.client.get("/en/prompts/historical-slug-811/")
        self.assertEqual(resp.status_code, 200)

    def test_review_without_live_snapshot_is_not_covered_by_historical_fallback(self):
        """The historical-fallback compat_qs is scoped to status=PUBLISHED
        only; a review-status prompt with no live_i18n entry for this
        language must not resolve via its current translation slug (it is
        already excluded from visible_in_language() by
        EditorialQuerySet.visible_on_site(), but this locks in that the
        resolver itself does not separately widen it back in)."""
        p = Prompt.objects.create(status=EditorialWorkflowMixin.STATUS_REVIEW, author=self.author)
        p.create_translation("en", title="Never Published", intro="i", body="b", outro="o",
                              slug="review-no-snapshot-811")
        self.assertEqual(p.live_i18n, {})

        resp = self.client.get("/en/prompts/review-no-snapshot-811/")
        self.assertEqual(resp.status_code, 404)
