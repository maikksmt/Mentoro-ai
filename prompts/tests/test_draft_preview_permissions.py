"""
Beta 11.5 group B: who may open a saved-draft preview.

Mirrors ``guides/tests/test_draft_preview_security.py`` (Beta 11.4). The
endpoint reuses the existing object-level editorial contract
(``EditorialWorkflowAdminMixin.has_change_permission``) rather than
re-deriving roles: Editor/Admin/superuser for any prompt, Author for their
own only, nobody else.

Everything that is not permitted answers 404, never 403 - a non-owning
author must not be able to use the endpoint to confirm that a prompt id
exists.
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from prompts.tests.draft_preview_fixtures import make_draft_prompt, make_user

User = get_user_model()


def preview_url(prompt_pk, language_code="en"):
    return reverse("admin:prompts_prompt_draft_preview", args=[prompt_pk, language_code])


class DraftPreviewPermissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = make_user("sec-owner", group="Author")
        cls.other_author = make_user("sec-other-author", group="Author")
        cls.editor = make_user("sec-editor", group="Editor")
        cls.admin_group = make_user("sec-admin-group", group="Admin")
        cls.superuser = make_user("sec-superuser", superuser=True)
        cls.roleless_staff = make_user("sec-roleless-staff")
        cls.plain_user = User.objects.create_user(
            username="sec-plain", password="pw", is_staff=False
        )

        cls.owned = make_draft_prompt(cls.owner, slug="sec-owned-en", title="Owned Prompt")
        cls.foreign = make_draft_prompt(
            cls.other_author, slug="sec-foreign-en", title="Foreign Prompt"
        )

    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)

    def test_anonymous_is_redirected_to_admin_login(self):
        resp = self.client.get(preview_url(self.owned.pk))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin/login/", resp.url)

    def test_plain_authenticated_user_is_redirected_to_admin_login(self):
        self.client.force_login(self.plain_user)
        resp = self.client.get(preview_url(self.owned.pk))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin/login/", resp.url)

    def test_staff_without_an_editorial_role_is_denied(self):
        self.client.force_login(self.roleless_staff)
        resp = self.client.get(preview_url(self.owned.pk))
        self.assertEqual(resp.status_code, 404)

    def test_author_may_preview_their_own_prompt(self):
        self.client.force_login(self.owner)
        resp = self.client.get(preview_url(self.owned.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Owned Prompt")

    def test_author_may_not_preview_someone_elses_prompt(self):
        self.client.force_login(self.owner)
        resp = self.client.get(preview_url(self.foreign.pk))
        self.assertEqual(resp.status_code, 404)

    def test_denied_response_does_not_disclose_the_draft(self):
        self.client.force_login(self.owner)
        resp = self.client.get(preview_url(self.foreign.pk))
        self.assertNotContains(resp, "Foreign Prompt", status_code=404)

    def test_editor_admin_and_superuser_may_preview_any_prompt(self):
        for label, user in (
            ("editor", self.editor),
            ("admin-group", self.admin_group),
            ("superuser", self.superuser),
        ):
            for prompt_label, prompt in (("owned", self.owned), ("foreign", self.foreign)):
                with self.subTest(role=label, prompt=prompt_label):
                    self.client.force_login(user)
                    resp = self.client.get(preview_url(prompt.pk))
                    self.assertEqual(resp.status_code, 200)
            self.client.logout()

    def test_guessing_a_neighbouring_id_does_not_leak_a_foreign_draft(self):
        """IDOR guard: the object-level check runs server-side, so walking
        ids as a non-owning author yields nothing but 404s."""
        self.client.force_login(self.owner)
        for pk in range(1, self.foreign.pk + 2):
            if pk == self.owned.pk:
                continue
            with self.subTest(pk=pk):
                resp = self.client.get(preview_url(pk))
                self.assertEqual(resp.status_code, 404)
