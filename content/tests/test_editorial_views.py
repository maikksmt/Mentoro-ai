"""
Coverage-Schritt 2: behavioral, permission, and workflow-transition tests for
content/views/editorial.py (my_content, submit_to_review, my_content_update,
review_queue, review_update) plus the small public module-level helpers
(get_editorial_model, is_author, is_editor).

Permission model under test (core/authz.py, via django-rules'
ObjectPermissionBackend):
  - content.submit_for_review: author OR editor
  - content.request_rework:    editor AND NOT author  (no self-review)
  - content.approve:           editor AND NOT author  (no self-approval)
  - content.publish:           author OR editor
  - content.archive:           author OR editor
  - content.restore:           author OR editor

Decorator order matters: `@require_group(...)` is applied *outside*
`@login_required` on submit_to_review/my_content_update/review_queue/
review_update, so it runs first on every request - an anonymous user hits
`require_group`'s own `if not u.is_authenticated: raise PermissionDenied`
before login_required ever gets a chance to redirect. That means these four
endpoints answer anonymous access with 403, not a login redirect - only
`my_content` (login_required only) redirects anonymous users to the login
page.
"""
import itertools

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin
from guides.models import Guide
from content.views.editorial import get_editorial_model, is_author, is_editor
from mentoroai.tests.utils import silence_django_request_warnings

User = get_user_model()

_slug_counter = itertools.count()


def _unique_slug(prefix):
    return f"{prefix}-{next(_slug_counter)}"


def make_guide(*, status, author=None):
    with translation.override("en"):
        g = Guide.objects.create(status=status, author=author)
        slug = _unique_slug("ed-guide")
        g.create_translation("en", title=f"Guide {slug}", intro="i", body="b", slug=slug)
    return g


class EditorialModuleHelperTests(TestCase):
    """Direct tests of the small public (non-underscore) module-level
    helpers - these are part of the module's public surface, not private
    implementation detail, and each has a real branch worth pinning."""

    def test_get_editorial_model_resolves_every_registered_key(self):
        from guides.models import Guide as GuideModel
        from prompts.models import Prompt
        from usecases.models import UseCase
        self.assertIs(get_editorial_model("guide"), GuideModel)
        self.assertIs(get_editorial_model("prompt"), Prompt)
        self.assertIs(get_editorial_model("usecase"), UseCase)
        self.assertIs(get_editorial_model("comparison"), Comparison)

    def test_get_editorial_model_raises_404_for_unknown_key(self):
        from django.http import Http404
        with self.assertRaises(Http404):
            get_editorial_model("not-a-real-model")

    def test_is_author_true_only_for_matching_author_id(self):
        u1 = User.objects.create_user(username="u1", password="pass")
        u2 = User.objects.create_user(username="u2", password="pass")
        guide = make_guide(status="draft", author=u1)
        self.assertTrue(is_author(u1, guide))
        self.assertFalse(is_author(u2, guide))

    def test_is_author_false_when_object_has_no_author(self):
        u1 = User.objects.create_user(username="u3", password="pass")
        guide = make_guide(status="draft", author=None)
        self.assertFalse(is_author(u1, guide))

    def test_is_editor_true_for_editor_and_admin_groups_false_otherwise(self):
        editor = User.objects.create_user(username="ed1", password="pass")
        editor.groups.add(Group.objects.get(name="Editor"))
        admin = User.objects.create_user(username="ad1", password="pass")
        admin.groups.add(Group.objects.get(name="Admin"))
        plain = User.objects.create_user(username="pl1", password="pass")
        self.assertTrue(is_editor(editor))
        self.assertTrue(is_editor(admin))
        self.assertFalse(is_editor(plain))


class MyContentAccessTests(TestCase):
    def test_anonymous_is_redirected_to_login(self):
        resp = self.client.get(reverse("content:editorial:my_content"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp.headers["Location"])

    def test_authenticated_user_without_any_group_can_view_own_page(self):
        User.objects.create_user(username="plain", password="pass")
        self.client.login(username="plain", password="pass")
        resp = self.client.get(reverse("content:editorial:my_content"))
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "content/editorial/my_content.html")


class MyContentScopeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user(username="author-a", password="pass")
        cls.other = User.objects.create_user(username="author-b", password="pass")
        cls.own_guide = make_guide(status=EditorialWorkflowMixin.STATUS_DRAFT, author=cls.author)
        cls.foreign_guide = make_guide(status=EditorialWorkflowMixin.STATUS_DRAFT, author=cls.other)
        cls.own_archived = make_guide(status=EditorialWorkflowMixin.STATUS_ARCHIVED, author=cls.author)

    def test_only_own_items_are_listed_not_other_authors_drafts(self):
        self.client.login(username="author-a", password="pass")
        resp = self.client.get(reverse("content:editorial:my_content"))
        items = resp.context["items"]
        ids = {obj.pk for _key, obj in items}
        self.assertIn(self.own_guide.pk, ids)
        self.assertNotIn(self.foreign_guide.pk, ids)

    def test_all_own_statuses_including_archived_are_listed(self):
        self.client.login(username="author-a", password="pass")
        resp = self.client.get(reverse("content:editorial:my_content"))
        ids = {obj.pk for _key, obj in resp.context["items"]}
        self.assertIn(self.own_archived.pk, ids)


class SubmitToReviewAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user(username="sub-author", password="pass")
        cls.author.groups.add(Group.objects.get(name="Author"))
        cls.no_group = User.objects.create_user(username="sub-nogroup", password="pass")

    def test_anonymous_gets_permission_denied_not_a_login_redirect(self):
        with silence_django_request_warnings():
            resp = self.client.post(reverse("content:editorial:submit_to_review"), {})
        self.assertEqual(resp.status_code, 403)

    def test_authenticated_without_required_group_is_forbidden(self):
        self.client.login(username="sub-nogroup", password="pass")
        with silence_django_request_warnings():
            resp = self.client.post(reverse("content:editorial:submit_to_review"), {})
        self.assertEqual(resp.status_code, 403)

    def test_get_is_not_allowed_and_redirects_without_side_effects(self):
        self.client.login(username="sub-author", password="pass")
        resp = self.client.get(reverse("content:editorial:submit_to_review"))
        self.assertRedirects(resp, reverse("content:editorial:my_content"))

    def test_invalid_form_shows_error_and_redirects(self):
        self.client.login(username="sub-author", password="pass")
        resp = self.client.post(
            reverse("content:editorial:submit_to_review"), {"model": "guide"}, follow=True,
        )
        self.assertContains(resp, "Invalid request.")

    def test_unknown_object_id_is_404(self):
        self.client.login(username="sub-author", password="pass")
        resp = self.client.post(
            reverse("content:editorial:submit_to_review"),
            {"model": "guide", "object_id": 9_999_999},
        )
        self.assertEqual(resp.status_code, 404)


class SubmitToReviewWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user(username="wf-author", password="pass")
        cls.author.groups.add(Group.objects.get(name="Author"))
        cls.other_author = User.objects.create_user(username="wf-other", password="pass")
        cls.other_author.groups.add(Group.objects.get(name="Author"))
        cls.editor = User.objects.create_user(username="wf-editor", password="pass")
        cls.editor.groups.add(Group.objects.get(name="Editor"))

    def test_author_submits_own_draft_to_review(self):
        guide = make_guide(status=EditorialWorkflowMixin.STATUS_DRAFT, author=self.author)
        self.client.login(username="wf-author", password="pass")
        resp = self.client.post(
            reverse("content:editorial:submit_to_review"),
            {"model": "guide", "object_id": guide.pk},
            follow=True,
        )
        # Re-fetch instead of refresh_from_db(): django-fsm's protected
        # status field refuses direct assignment (see Guide.status FSMField).
        guide = Guide.objects.get(pk=guide.pk)
        self.assertEqual(guide.status, EditorialWorkflowMixin.STATUS_REVIEW)
        self.assertContains(resp, "Status updated.")

    def test_author_cannot_submit_a_foreign_draft_no_idor(self):
        foreign_guide = make_guide(status=EditorialWorkflowMixin.STATUS_DRAFT, author=self.other_author)
        self.client.login(username="wf-author", password="pass")
        resp = self.client.post(
            reverse("content:editorial:submit_to_review"),
            {"model": "guide", "object_id": foreign_guide.pk},
            follow=True,
        )
        foreign_guide = Guide.objects.get(pk=foreign_guide.pk)
        self.assertEqual(foreign_guide.status, EditorialWorkflowMixin.STATUS_DRAFT)
        self.assertContains(resp, "You are not allowed to submit this item for review.")

    def test_editor_can_submit_someone_elses_draft(self):
        guide = make_guide(status=EditorialWorkflowMixin.STATUS_DRAFT, author=self.other_author)
        self.client.login(username="wf-editor", password="pass")
        resp = self.client.post(
            reverse("content:editorial:submit_to_review"),
            {"model": "guide", "object_id": guide.pk},
            follow=True,
        )
        # Re-fetch instead of refresh_from_db(): django-fsm's protected
        # status field refuses direct assignment (see Guide.status FSMField).
        guide = Guide.objects.get(pk=guide.pk)
        self.assertEqual(guide.status, EditorialWorkflowMixin.STATUS_REVIEW)
        self.assertContains(resp, "Status updated.")

    def test_already_in_review_cannot_be_resubmitted(self):
        guide = make_guide(status=EditorialWorkflowMixin.STATUS_REVIEW, author=self.author)
        self.client.login(username="wf-author", password="pass")
        resp = self.client.post(
            reverse("content:editorial:submit_to_review"),
            {"model": "guide", "object_id": guide.pk},
            follow=True,
        )
        # Re-fetch instead of refresh_from_db(): django-fsm's protected
        # status field refuses direct assignment (see Guide.status FSMField).
        guide = Guide.objects.get(pk=guide.pk)
        self.assertEqual(guide.status, EditorialWorkflowMixin.STATUS_REVIEW)
        self.assertContains(resp, "Transition not allowed from current state.")


class MyContentUpdateAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user(username="upd-author", password="pass")
        cls.author.groups.add(Group.objects.get(name="Author"))
        cls.no_group = User.objects.create_user(username="upd-nogroup", password="pass")

    def test_anonymous_gets_permission_denied(self):
        with silence_django_request_warnings():
            resp = self.client.post(reverse("content:editorial:my_content_update"), {})
        self.assertEqual(resp.status_code, 403)

    def test_authenticated_without_group_is_forbidden(self):
        self.client.login(username="upd-nogroup", password="pass")
        with silence_django_request_warnings():
            resp = self.client.post(reverse("content:editorial:my_content_update"), {})
        self.assertEqual(resp.status_code, 403)

    def test_get_redirects_without_side_effects(self):
        self.client.login(username="upd-author", password="pass")
        resp = self.client.get(reverse("content:editorial:my_content_update"))
        self.assertRedirects(resp, reverse("content:editorial:my_content"))

    def test_invalid_status_choice_shows_error(self):
        self.client.login(username="upd-author", password="pass")
        resp = self.client.post(
            reverse("content:editorial:my_content_update"),
            {"model": "guide", "object_id": 1, "status": "not-a-real-status"},
            follow=True,
        )
        self.assertContains(resp, "Invalid request.")


class MyContentUpdateWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user(username="upd-wf-author", password="pass")
        cls.author.groups.add(Group.objects.get(name="Author"))

    def _post(self, guide, status):
        return self.client.post(
            reverse("content:editorial:my_content_update"),
            {"model": "guide", "object_id": guide.pk, "status": status},
            follow=True,
        )

    def test_author_publishes_own_approved_guide(self):
        guide = make_guide(status=EditorialWorkflowMixin.STATUS_APPROVED, author=self.author)
        self.client.login(username="upd-wf-author", password="pass")
        resp = self._post(guide, "published")
        # Re-fetch instead of refresh_from_db(): django-fsm's protected
        # status field refuses direct assignment (see Guide.status FSMField).
        guide = Guide.objects.get(pk=guide.pk)
        self.assertEqual(guide.status, EditorialWorkflowMixin.STATUS_PUBLISHED)
        self.assertIsNotNone(guide.published_at)
        self.assertContains(resp, "Status updated.")

    def test_author_archives_own_published_guide(self):
        guide = make_guide(status=EditorialWorkflowMixin.STATUS_PUBLISHED, author=self.author)
        self.client.login(username="upd-wf-author", password="pass")
        resp = self._post(guide, "archived")
        # Re-fetch instead of refresh_from_db(): django-fsm's protected
        # status field refuses direct assignment (see Guide.status FSMField).
        guide = Guide.objects.get(pk=guide.pk)
        self.assertEqual(guide.status, EditorialWorkflowMixin.STATUS_ARCHIVED)
        self.assertContains(resp, "Status updated.")

    def test_author_restores_own_archived_guide_to_draft(self):
        guide = make_guide(status=EditorialWorkflowMixin.STATUS_ARCHIVED, author=self.author)
        self.client.login(username="upd-wf-author", password="pass")
        resp = self._post(guide, "draft")
        # Re-fetch instead of refresh_from_db(): django-fsm's protected
        # status field refuses direct assignment (see Guide.status FSMField).
        guide = Guide.objects.get(pk=guide.pk)
        self.assertEqual(guide.status, EditorialWorkflowMixin.STATUS_DRAFT)
        self.assertContains(resp, "Status updated.")

    def test_restore_fails_from_a_non_archived_source_state(self):
        guide = make_guide(status=EditorialWorkflowMixin.STATUS_PUBLISHED, author=self.author)
        self.client.login(username="upd-wf-author", password="pass")
        resp = self._post(guide, "draft")
        # Re-fetch instead of refresh_from_db(): django-fsm's protected
        # status field refuses direct assignment (see Guide.status FSMField).
        guide = Guide.objects.get(pk=guide.pk)
        self.assertEqual(guide.status, EditorialWorkflowMixin.STATUS_PUBLISHED)
        self.assertContains(resp, "Transition not allowed from current state.")

    def test_author_alone_cannot_approve_own_review_item_no_self_approval(self):
        guide = make_guide(status=EditorialWorkflowMixin.STATUS_REVIEW, author=self.author)
        self.client.login(username="upd-wf-author", password="pass")
        resp = self._post(guide, "approved")
        # Re-fetch instead of refresh_from_db(): django-fsm's protected
        # status field refuses direct assignment (see Guide.status FSMField).
        guide = Guide.objects.get(pk=guide.pk)
        self.assertEqual(guide.status, EditorialWorkflowMixin.STATUS_REVIEW)
        self.assertContains(resp, "You are not allowed to perform this transition.")

    def test_author_alone_cannot_request_rework_on_own_review_item(self):
        guide = make_guide(status=EditorialWorkflowMixin.STATUS_REVIEW, author=self.author)
        self.client.login(username="upd-wf-author", password="pass")
        resp = self._post(guide, "rework")
        # Re-fetch instead of refresh_from_db(): django-fsm's protected
        # status field refuses direct assignment (see Guide.status FSMField).
        guide = Guide.objects.get(pk=guide.pk)
        self.assertEqual(guide.status, EditorialWorkflowMixin.STATUS_REVIEW)
        self.assertContains(resp, "You are not allowed to perform this transition.")


class ReviewQueueAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.editor = User.objects.create_user(username="rq-editor", password="pass")
        cls.editor.groups.add(Group.objects.get(name="Editor"))
        cls.author_only = User.objects.create_user(username="rq-author", password="pass")
        cls.author_only.groups.add(Group.objects.get(name="Author"))

    def test_anonymous_gets_permission_denied(self):
        with silence_django_request_warnings():
            resp = self.client.get(reverse("content:editorial:review_queue"))
        self.assertEqual(resp.status_code, 403)

    def test_author_only_group_is_forbidden(self):
        self.client.login(username="rq-author", password="pass")
        with silence_django_request_warnings():
            resp = self.client.get(reverse("content:editorial:review_queue"))
        self.assertEqual(resp.status_code, 403)

    def test_editor_can_view_review_queue(self):
        self.client.login(username="rq-editor", password="pass")
        resp = self.client.get(reverse("content:editorial:review_queue"))
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "content/editorial/review_queue.html")


class ReviewQueueContentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.editor = User.objects.create_user(username="rq2-editor", password="pass")
        cls.editor.groups.add(Group.objects.get(name="Editor"))
        cls.author = User.objects.create_user(username="rq2-author", password="pass")
        cls.in_review = make_guide(status=EditorialWorkflowMixin.STATUS_REVIEW, author=cls.author)
        cls.in_draft = make_guide(status=EditorialWorkflowMixin.STATUS_DRAFT, author=cls.author)

    def test_only_review_status_items_appear_across_all_authors(self):
        self.client.login(username="rq2-editor", password="pass")
        resp = self.client.get(reverse("content:editorial:review_queue"))
        queues = dict(resp.context["queues"])
        review_ids = {obj.pk for obj in queues["guide"]}
        self.assertIn(self.in_review.pk, review_ids)
        self.assertNotIn(self.in_draft.pk, review_ids)


class ReviewUpdateAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.editor = User.objects.create_user(username="ru-editor", password="pass")
        cls.editor.groups.add(Group.objects.get(name="Editor"))
        cls.author_only = User.objects.create_user(username="ru-author", password="pass")
        cls.author_only.groups.add(Group.objects.get(name="Author"))

    def test_anonymous_gets_permission_denied(self):
        with silence_django_request_warnings():
            resp = self.client.post(reverse("content:editorial:review_update"), {})
        self.assertEqual(resp.status_code, 403)

    def test_author_only_group_is_forbidden(self):
        self.client.login(username="ru-author", password="pass")
        with silence_django_request_warnings():
            resp = self.client.post(reverse("content:editorial:review_update"), {})
        self.assertEqual(resp.status_code, 403)

    def test_get_redirects_to_review_queue_without_side_effects(self):
        self.client.login(username="ru-editor", password="pass")
        resp = self.client.get(reverse("content:editorial:review_update"))
        self.assertRedirects(resp, reverse("content:editorial:review_queue"))

    def test_unknown_object_id_is_404(self):
        self.client.login(username="ru-editor", password="pass")
        resp = self.client.post(
            reverse("content:editorial:review_update"),
            {"model": "guide", "object_id": 9_999_999, "status": "approved"},
        )
        self.assertEqual(resp.status_code, 404)


class ReviewUpdateWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.editor = User.objects.create_user(username="ru-wf-editor", password="pass")
        cls.editor.groups.add(Group.objects.get(name="Editor"))
        cls.author = User.objects.create_user(username="ru-wf-author", password="pass")

    def _post(self, guide, status):
        return self.client.post(
            reverse("content:editorial:review_update"),
            {"model": "guide", "object_id": guide.pk, "status": status},
            follow=True,
        )

    def test_editor_approves_someone_elses_review_item(self):
        guide = make_guide(status=EditorialWorkflowMixin.STATUS_REVIEW, author=self.author)
        self.client.login(username="ru-wf-editor", password="pass")
        resp = self._post(guide, "approved")
        # Re-fetch instead of refresh_from_db(): django-fsm's protected
        # status field refuses direct assignment (see Guide.status FSMField).
        guide = Guide.objects.get(pk=guide.pk)
        self.assertEqual(guide.status, EditorialWorkflowMixin.STATUS_APPROVED)
        self.assertEqual(guide.reviewed_by_id, self.editor.id)
        self.assertContains(resp, "Status updated.")

    def test_editor_cannot_approve_their_own_authored_item_no_self_approval(self):
        own_guide = make_guide(status=EditorialWorkflowMixin.STATUS_REVIEW, author=self.editor)
        self.client.login(username="ru-wf-editor", password="pass")
        resp = self._post(own_guide, "approved")
        own_guide = Guide.objects.get(pk=own_guide.pk)
        self.assertEqual(own_guide.status, EditorialWorkflowMixin.STATUS_REVIEW)
        self.assertContains(resp, "You are not allowed to perform this transition.")

    def test_editor_requests_rework_on_someone_elses_review_item(self):
        guide = make_guide(status=EditorialWorkflowMixin.STATUS_REVIEW, author=self.author)
        self.client.login(username="ru-wf-editor", password="pass")
        resp = self._post(guide, "rework")
        # Re-fetch instead of refresh_from_db(): django-fsm's protected
        # status field refuses direct assignment (see Guide.status FSMField).
        guide = Guide.objects.get(pk=guide.pk)
        self.assertEqual(guide.status, EditorialWorkflowMixin.STATUS_REWORK)
        self.assertEqual(guide.reviewed_by_id, self.editor.id)
        self.assertContains(resp, "Status updated.")

    def test_invalid_form_shows_error_and_redirects_to_review_queue(self):
        self.client.login(username="ru-wf-editor", password="pass")
        resp = self.client.post(
            reverse("content:editorial:review_update"),
            {"model": "guide"},
            follow=True,
        )
        self.assertContains(resp, "Invalid request.")
