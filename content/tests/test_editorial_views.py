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
import ast
import itertools
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import translation
from reversion.models import Revision

from catalog.models import Tool
from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin
from core.review_binding import fingerprint_review_payload
from guides.models import Guide
from content.views.editorial import get_editorial_model, is_author, is_editor
from mentoroai.tests.utils import silence_django_request_warnings
from prompts.models import Prompt, PromptTranslation
from prompts.review_approval import PromptReviewApprovalError, PromptReviewApprovalErrorCode
from prompts.review_payload import build_prompt_review_payload
from usecases.models import UseCase

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


def make_prompt(*, status=EditorialWorkflowMixin.STATUS_DRAFT, author=None, languages=("en",)):
    with translation.override("en"):
        p = Prompt.objects.create(status=status, author=author)
        for lang in languages:
            p.create_translation(
                lang, title=f"Prompt {_unique_slug(lang)}", intro="i", body="b", outro="o",
                slug=_unique_slug("ed-prompt"),
            )
    return p


def refetch_prompt(prompt):
    """django-fsm's protected ``status`` descriptor rejects the ``setattr``
    ``refresh_from_db()`` performs, so reload through the manager."""
    return Prompt.objects.get(pk=prompt.pk)


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


# ======================================================================
# Beta 11.11C4B: Prompt submit/approval bound to the C2A/C3A primitives
# ======================================================================
#
# Guide/UseCase/Comparison keep the generic FSM + obj.save() path exercised
# above unchanged. These tests cover exclusively the Prompt-specific branches
# added in content/views/editorial.py::_submit_prompt_for_review_via_primitive
# / _approve_prompt_review_via_primitive.


class PromptSubmitViaEditorialViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user(username="prompt-sub-author", password="pass")
        cls.author.groups.add(Group.objects.get(name="Author"))

    def test_full_draft_prompt_submits_with_full_binding_contract(self):
        tool = Tool.objects.create(slug=_unique_slug("psub-tool"))
        tool.create_translation("en", name="Tool")
        prompt = make_prompt(author=self.author, languages=("en", "de"))
        prompt.tags.add("alpha")
        prompt.tools.add(tool)

        expected_fp = fingerprint_review_payload(build_prompt_review_payload(refetch_prompt(prompt)))
        revisions_before = Revision.objects.count()

        self.client.login(username="prompt-sub-author", password="pass")
        resp = self.client.post(
            reverse("content:editorial:submit_to_review"),
            {"model": "prompt", "object_id": prompt.pk},
            follow=True,
        )

        self.assertRedirects(resp, reverse("content:editorial:my_content"))
        self.assertContains(resp, "Status updated.")

        reloaded = refetch_prompt(prompt)
        self.assertEqual(reloaded.status, EditorialWorkflowMixin.STATUS_REVIEW)
        self.assertIsNotNone(reloaded.review_revision_id)
        self.assertEqual(reloaded.review_payload_fingerprint, expected_fp)
        self.assertIsNone(reloaded.approved_revision_id)
        self.assertIsNotNone(reloaded.submitted_for_review_at)

        self.assertEqual(Revision.objects.count(), revisions_before + 1)
        revision = Revision.objects.get(pk=reloaded.review_revision_id)
        self.assertEqual(revision.user_id, self.author.pk)
        self.assertEqual(revision.comment, "submit_for_review")
        labels = {
            f"{v.content_type.app_label}.{v.content_type.model}"
            for v in revision.version_set.all()
        }
        self.assertEqual(labels, {"prompts.prompt", "prompts.prompttranslation"})

    def test_foreign_author_cannot_submit_someone_elses_prompt_no_idor(self):
        other_author = User.objects.create_user(username="prompt-other-author", password="pass")
        other_author.groups.add(Group.objects.get(name="Author"))
        prompt = make_prompt(author=other_author)

        with mock.patch("content.views.editorial.submit_prompt_for_review") as submit_mock:
            self.client.login(username="prompt-sub-author", password="pass")
            resp = self.client.post(
                reverse("content:editorial:submit_to_review"),
                {"model": "prompt", "object_id": prompt.pk},
                follow=True,
            )
        submit_mock.assert_not_called()
        self.assertEqual(refetch_prompt(prompt).status, EditorialWorkflowMixin.STATUS_DRAFT)
        self.assertContains(resp, "You are not allowed to submit this item for review.")

    def test_submit_status_matrix(self):
        for status, expect_success in (
            (EditorialWorkflowMixin.STATUS_DRAFT, True),
            (EditorialWorkflowMixin.STATUS_REWORK, True),
            (EditorialWorkflowMixin.STATUS_REVIEW, False),
            (EditorialWorkflowMixin.STATUS_APPROVED, False),
            (EditorialWorkflowMixin.STATUS_PUBLISHED, False),
            (EditorialWorkflowMixin.STATUS_ARCHIVED, False),
        ):
            with self.subTest(status=status):
                prompt = make_prompt(author=self.author)
                Prompt.objects.filter(pk=prompt.pk).update(status=status)
                revisions_before = Revision.objects.count()
                self.client.login(username="prompt-sub-author", password="pass")
                resp = self.client.post(
                    reverse("content:editorial:submit_to_review"),
                    {"model": "prompt", "object_id": prompt.pk},
                    follow=True,
                )
                reloaded = refetch_prompt(prompt)
                if expect_success:
                    self.assertEqual(reloaded.status, EditorialWorkflowMixin.STATUS_REVIEW)
                    self.assertEqual(Revision.objects.count(), revisions_before + 1)
                else:
                    self.assertEqual(reloaded.status, status)
                    self.assertEqual(Revision.objects.count(), revisions_before)
                    self.assertContains(resp, "Transition not allowed from current state.")
                self.client.logout()


class PromptApprovalViaEditorialViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user(username="prompt-appr-author", password="pass")
        cls.author.groups.add(Group.objects.get(name="Author"))
        cls.editor = User.objects.create_user(username="prompt-appr-editor", password="pass")
        cls.editor.groups.add(Group.objects.get(name="Editor"))

    def _submitted_prompt(self, *, author=None):
        prompt = make_prompt(author=author or self.author)
        self.client.login(username=(author or self.author).username, password="pass")
        self.client.post(
            reverse("content:editorial:submit_to_review"),
            {"model": "prompt", "object_id": prompt.pk},
        )
        self.client.logout()
        return refetch_prompt(prompt)

    def test_editor_approves_someone_elses_submitted_prompt(self):
        prompt = self._submitted_prompt()
        review_revision_id = prompt.review_revision_id
        fingerprint = prompt.review_payload_fingerprint

        revisions_before = Revision.objects.count()
        self.client.login(username="prompt-appr-editor", password="pass")
        resp = self.client.post(
            reverse("content:editorial:review_update"),
            {"model": "prompt", "object_id": prompt.pk, "status": "approved"},
            follow=True,
        )

        self.assertRedirects(resp, reverse("content:editorial:review_queue"))
        self.assertContains(resp, "Status updated.")

        reloaded = refetch_prompt(prompt)
        self.assertEqual(reloaded.status, EditorialWorkflowMixin.STATUS_APPROVED)
        self.assertEqual(reloaded.review_revision_id, review_revision_id)
        self.assertEqual(reloaded.approved_revision_id, review_revision_id)
        self.assertEqual(reloaded.review_payload_fingerprint, fingerprint)
        self.assertEqual(reloaded.reviewed_by_id, self.editor.pk)
        self.assertIsNotNone(reloaded.reviewed_at)

        self.assertEqual(Revision.objects.count(), revisions_before + 1)
        audit_revision = Revision.objects.filter(comment="approve").latest("pk")
        self.assertEqual(audit_revision.user_id, self.editor.pk)
        labels = {
            f"{v.content_type.app_label}.{v.content_type.model}"
            for v in audit_revision.version_set.all()
        }
        self.assertEqual(labels, {"prompts.prompt", "prompts.prompttranslation"})
        # the audit revision is never the approval binding itself
        self.assertNotEqual(audit_revision.pk, reloaded.approved_revision_id)

    def test_editor_cannot_approve_their_own_submitted_prompt_no_self_approval(self):
        prompt = self._submitted_prompt(author=self.editor)

        with mock.patch("content.views.editorial.approve_prompt_review") as approve_mock:
            self.client.login(username="prompt-appr-editor", password="pass")
            resp = self.client.post(
                reverse("content:editorial:review_update"),
                {"model": "prompt", "object_id": prompt.pk, "status": "approved"},
                follow=True,
            )
        approve_mock.assert_not_called()
        self.assertEqual(refetch_prompt(prompt).status, EditorialWorkflowMixin.STATUS_REVIEW)
        self.assertIsNone(refetch_prompt(prompt).approved_revision_id)
        self.assertContains(resp, "You are not allowed to perform this transition.")

    def test_approval_status_matrix(self):
        for status, expect_success in (
            (EditorialWorkflowMixin.STATUS_DRAFT, False),
            (EditorialWorkflowMixin.STATUS_REWORK, False),
            (EditorialWorkflowMixin.STATUS_REVIEW, True),
            (EditorialWorkflowMixin.STATUS_APPROVED, False),
            (EditorialWorkflowMixin.STATUS_PUBLISHED, False),
            (EditorialWorkflowMixin.STATUS_ARCHIVED, False),
        ):
            with self.subTest(status=status):
                if status == EditorialWorkflowMixin.STATUS_REVIEW:
                    prompt = self._submitted_prompt()
                else:
                    prompt = make_prompt(author=self.author)
                    Prompt.objects.filter(pk=prompt.pk).update(status=status)
                revisions_before = Revision.objects.count()
                self.client.login(username="prompt-appr-editor", password="pass")
                self.client.post(
                    reverse("content:editorial:review_update"),
                    {"model": "prompt", "object_id": prompt.pk, "status": "approved"},
                    follow=True,
                )
                reloaded = refetch_prompt(prompt)
                if expect_success:
                    self.assertEqual(reloaded.status, EditorialWorkflowMixin.STATUS_APPROVED)
                    self.assertEqual(Revision.objects.count(), revisions_before + 1)
                else:
                    self.assertEqual(reloaded.status, status)
                    self.assertEqual(Revision.objects.count(), revisions_before)
                self.client.logout()


class PromptStaleApprovalViaEditorialViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user(username="prompt-stale-author", password="pass")
        cls.author.groups.add(Group.objects.get(name="Author"))
        cls.editor = User.objects.create_user(username="prompt-stale-editor", password="pass")
        cls.editor.groups.add(Group.objects.get(name="Editor"))

    def _submitted_prompt(self):
        prompt = make_prompt(author=self.author)
        self.client.login(username="prompt-stale-author", password="pass")
        self.client.post(
            reverse("content:editorial:submit_to_review"),
            {"model": "prompt", "object_id": prompt.pk},
        )
        self.client.logout()
        return refetch_prompt(prompt)

    def _assert_blocked(self, prompt):
        before = refetch_prompt(prompt)
        revisions_before = Revision.objects.count()
        self.client.login(username="prompt-stale-editor", password="pass")
        resp = self.client.post(
            reverse("content:editorial:review_update"),
            {"model": "prompt", "object_id": prompt.pk, "status": "approved"},
            follow=True,
        )
        reloaded = refetch_prompt(prompt)
        self.assertEqual(reloaded.status, EditorialWorkflowMixin.STATUS_REVIEW)
        self.assertEqual(reloaded.review_revision_id, before.review_revision_id)
        self.assertIsNone(reloaded.approved_revision_id)
        self.assertEqual(reloaded.review_payload_fingerprint, before.review_payload_fingerprint)
        self.assertEqual(Revision.objects.count(), revisions_before)
        self.assertContains(
            resp,
            "The reviewed prompt content has changed. Submit it for review "
            "again before approval.",
        )
        self.client.logout()

    def test_translation_changed_after_submit_blocks_approval(self):
        prompt = self._submitted_prompt()
        PromptTranslation.objects.filter(master_id=prompt.pk, language_code="en").update(
            title="changed after submit"
        )
        self._assert_blocked(prompt)

    def test_tag_membership_changed_after_submit_blocks_approval(self):
        prompt = self._submitted_prompt()
        prompt.tags.add("stale-view-tag")
        self._assert_blocked(prompt)

    def test_tool_membership_changed_after_submit_blocks_approval(self):
        prompt = self._submitted_prompt()
        tool = Tool.objects.create(slug=_unique_slug("stale-view-tool"))
        tool.create_translation("en", name="Stale Tool")
        prompt.tools.add(tool)
        self._assert_blocked(prompt)


class PromptCorruptBindingViaEditorialViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user(username="prompt-corrupt-author", password="pass")
        cls.author.groups.add(Group.objects.get(name="Author"))
        cls.editor = User.objects.create_user(username="prompt-corrupt-editor", password="pass")
        cls.editor.groups.add(Group.objects.get(name="Editor"))

    def _submitted_prompt(self):
        prompt = make_prompt(author=self.author)
        self.client.login(username="prompt-corrupt-author", password="pass")
        self.client.post(
            reverse("content:editorial:submit_to_review"),
            {"model": "prompt", "object_id": prompt.pk},
        )
        self.client.logout()
        return refetch_prompt(prompt)

    def test_review_revision_pointing_elsewhere_is_not_swallowed(self):
        a = self._submitted_prompt()
        b = self._submitted_prompt()
        Prompt.objects.filter(pk=a.pk).update(review_revision_id=b.review_revision_id)

        revisions_before = Revision.objects.count()
        self.client.login(username="prompt-corrupt-editor", password="pass")
        with self.assertRaises(PromptReviewApprovalError) as ctx:
            self.client.post(
                reverse("content:editorial:review_update"),
                {"model": "prompt", "object_id": a.pk, "status": "approved"},
                follow=False,
            )
        self.assertEqual(ctx.exception.code, PromptReviewApprovalErrorCode.REVIEW_BINDING_INVALID)
        self.assertEqual(refetch_prompt(a).status, EditorialWorkflowMixin.STATUS_REVIEW)
        self.assertEqual(Revision.objects.count(), revisions_before)

    def test_already_set_approved_revision_in_review_status_is_not_swallowed(self):
        prompt = self._submitted_prompt()
        Prompt.objects.filter(pk=prompt.pk).update(approved_revision_id=prompt.review_revision_id)

        self.client.login(username="prompt-corrupt-editor", password="pass")
        with self.assertRaises(PromptReviewApprovalError) as ctx:
            self.client.post(
                reverse("content:editorial:review_update"),
                {"model": "prompt", "object_id": prompt.pk, "status": "approved"},
                follow=False,
            )
        self.assertEqual(ctx.exception.code, PromptReviewApprovalErrorCode.REVIEW_BINDING_INVALID)


class PromptOtherTransitionsRemainGenericTests(TestCase):
    """
    C4B changes only the Prompt "review"/"approved" branches. Request rework
    (and, by the same unchanged code path, publish/archive/restore) for a
    Prompt still runs the generic FSM + obj.save() path this module has
    always used - proven here, not just asserted.
    """

    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user(username="prompt-generic-author", password="pass")
        cls.author.groups.add(Group.objects.get(name="Author"))
        cls.editor = User.objects.create_user(username="prompt-generic-editor", password="pass")
        cls.editor.groups.add(Group.objects.get(name="Editor"))

    def test_request_rework_for_prompt_still_uses_the_generic_path(self):
        prompt = make_prompt(author=self.author)
        self.client.login(username="prompt-generic-author", password="pass")
        self.client.post(
            reverse("content:editorial:submit_to_review"),
            {"model": "prompt", "object_id": prompt.pk},
        )
        self.client.logout()

        self.client.login(username="prompt-generic-editor", password="pass")
        resp = self.client.post(
            reverse("content:editorial:review_update"),
            {"model": "prompt", "object_id": prompt.pk, "status": "rework"},
            follow=True,
        )
        reloaded = refetch_prompt(prompt)
        self.assertEqual(reloaded.status, EditorialWorkflowMixin.STATUS_REWORK)
        self.assertEqual(reloaded.reviewed_by_id, self.editor.pk)
        self.assertContains(resp, "Status updated.")


class PromptEditorialViewNoDirectFSMCallTests(TestCase):
    def _module_tree(self):
        import pathlib

        import content.views.editorial as editorial_views_module

        source = pathlib.Path(editorial_views_module.__file__).read_text(encoding="utf-8")
        return ast.parse(source), source

    def _function_node(self, tree, name):
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        return None

    def test_prompt_helpers_never_call_forbidden_c2a_c3a_internals(self):
        tree, _source = self._module_tree()
        forbidden = {
            "create_revision", "set_user", "set_comment",
            "build_prompt_review_payload", "fingerprint_review_payload",
            "validate_review_binding", "validate_approved_binding",
            "revision_contains_object", "invalidate_editorial_review_state",
        }
        for fn_name in (
            "_submit_prompt_for_review_via_primitive",
            "_approve_prompt_review_via_primitive",
        ):
            node = self._function_node(tree, fn_name)
            self.assertIsNotNone(node, fn_name)
            offenders = []
            for n in ast.walk(node):
                if isinstance(n, ast.Call):
                    func = n.func
                    name = getattr(func, "attr", None) or getattr(func, "id", None)
                    if name in forbidden:
                        offenders.append(name)
            self.assertEqual(offenders, [], fn_name)

    def test_prompt_helpers_never_call_move_to_review_or_approve_directly(self):
        """
        Guide/UseCase/Comparison (and Prompt's own rework/publish/archive/
        restore branches, deliberately left alone) still dispatch dynamically
        via ``getattr(obj, method_name)(by=request.user)`` - never a literal
        ``.move_to_review(``/``.approve(`` - so checking for that literal
        attribute access inside the two Prompt helper functions is a precise,
        non-fragile guarantee that they never fall back to the direct FSM
        call themselves.
        """
        tree, _source = self._module_tree()
        for fn_name in (
            "_submit_prompt_for_review_via_primitive",
            "_approve_prompt_review_via_primitive",
        ):
            node = self._function_node(tree, fn_name)
            self.assertIsNotNone(node, fn_name)
            offenders = [
                n.attr
                for n in ast.walk(node)
                if isinstance(n, ast.Attribute) and n.attr in {"move_to_review", "approve"}
            ]
            self.assertEqual(offenders, [], fn_name)

    def test_module_uses_the_public_prompt_primitives(self):
        _tree, source = self._module_tree()
        self.assertIn("submit_prompt_for_review", source)
        self.assertIn("approve_prompt_review", source)


class OtherEditorialTypesUseTheGenericPathTests(TestCase):
    """
    Guide/UseCase/Comparison must never reach the Prompt-only primitives even
    though every type shares the same module-level STATUS_TRANSITIONS
    dispatch table.
    """

    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user(username="other-types-author", password="pass")
        cls.author.groups.add(Group.objects.get(name="Author"))
        cls.editor = User.objects.create_user(username="other-types-editor", password="pass")
        cls.editor.groups.add(Group.objects.get(name="Editor"))

    def _make(self, model, *, status=EditorialWorkflowMixin.STATUS_DRAFT):
        obj = model.objects.create(author=self.author)
        obj.create_translation(
            "en", title=f"T {_unique_slug('other')}", intro="i", body="b",
            slug=_unique_slug("other-type"),
        )
        if status != EditorialWorkflowMixin.STATUS_DRAFT:
            model.objects.filter(pk=obj.pk).update(status=status)
        return obj

    def test_guide_submit_never_calls_the_prompt_primitive(self):
        guide = self._make(Guide)
        with mock.patch("content.views.editorial.submit_prompt_for_review") as submit_mock:
            self.client.login(username="other-types-author", password="pass")
            self.client.post(
                reverse("content:editorial:submit_to_review"),
                {"model": "guide", "object_id": guide.pk},
            )
        submit_mock.assert_not_called()
        self.assertEqual(Guide.objects.get(pk=guide.pk).status, EditorialWorkflowMixin.STATUS_REVIEW)

    def test_usecase_submit_never_calls_the_prompt_primitive(self):
        obj = self._make(UseCase)
        with mock.patch("content.views.editorial.submit_prompt_for_review") as submit_mock:
            self.client.login(username="other-types-author", password="pass")
            self.client.post(
                reverse("content:editorial:submit_to_review"),
                {"model": "usecase", "object_id": obj.pk},
            )
        submit_mock.assert_not_called()
        self.assertEqual(UseCase.objects.get(pk=obj.pk).status, EditorialWorkflowMixin.STATUS_REVIEW)

    def test_comparison_submit_never_calls_the_prompt_primitive(self):
        comparison = self._make(Comparison)
        with mock.patch("content.views.editorial.submit_prompt_for_review") as submit_mock:
            self.client.login(username="other-types-author", password="pass")
            self.client.post(
                reverse("content:editorial:submit_to_review"),
                {"model": "comparison", "object_id": comparison.pk},
            )
        submit_mock.assert_not_called()
        self.assertEqual(
            Comparison.objects.get(pk=comparison.pk).status, EditorialWorkflowMixin.STATUS_REVIEW
        )

    def test_guide_approval_never_calls_the_prompt_primitive(self):
        guide = self._make(Guide, status=EditorialWorkflowMixin.STATUS_REVIEW)
        with mock.patch("content.views.editorial.approve_prompt_review") as approve_mock:
            self.client.login(username="other-types-editor", password="pass")
            self.client.post(
                reverse("content:editorial:review_update"),
                {"model": "guide", "object_id": guide.pk, "status": "approved"},
            )
        approve_mock.assert_not_called()
        self.assertEqual(Guide.objects.get(pk=guide.pk).status, EditorialWorkflowMixin.STATUS_APPROVED)

    def test_usecase_approval_never_calls_the_prompt_primitive(self):
        obj = self._make(UseCase, status=EditorialWorkflowMixin.STATUS_REVIEW)
        with mock.patch("content.views.editorial.approve_prompt_review") as approve_mock:
            self.client.login(username="other-types-editor", password="pass")
            self.client.post(
                reverse("content:editorial:review_update"),
                {"model": "usecase", "object_id": obj.pk, "status": "approved"},
            )
        approve_mock.assert_not_called()
        self.assertEqual(UseCase.objects.get(pk=obj.pk).status, EditorialWorkflowMixin.STATUS_APPROVED)

    def test_comparison_approval_never_calls_the_prompt_primitive(self):
        comparison = self._make(Comparison, status=EditorialWorkflowMixin.STATUS_REVIEW)
        with mock.patch("content.views.editorial.approve_prompt_review") as approve_mock:
            self.client.login(username="other-types-editor", password="pass")
            self.client.post(
                reverse("content:editorial:review_update"),
                {"model": "comparison", "object_id": comparison.pk, "status": "approved"},
            )
        approve_mock.assert_not_called()
        self.assertEqual(
            Comparison.objects.get(pk=comparison.pk).status, EditorialWorkflowMixin.STATUS_APPROVED
        )
