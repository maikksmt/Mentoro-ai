"""
Beta 11.8 group B: who may open a saved-draft preview.

Mirrors ``prompts/tests/test_draft_preview_permissions.py`` (Beta 11.5) and
``guides/tests/test_draft_preview_security.py`` (Beta 11.4). The endpoint
reuses the existing object-level editorial contract
(``EditorialWorkflowAdminMixin.has_change_permission``) rather than
re-deriving roles: Editor/Admin/superuser for any use case, Author for their
own only, nobody else.

Everything that is not permitted answers 404, never 403 - a non-owning
author must not be able to use the endpoint to confirm that a use case id
exists.
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from core.tests.idor_probes import build_bounded_idor_probe_ids
from usecases.models import UseCase
from usecases.tests.draft_preview_fixtures import make_draft_usecase, make_user

User = get_user_model()


def preview_url(usecase_pk, language_code="en"):
    return reverse("admin:usecases_usecase_draft_preview", args=[usecase_pk, language_code])


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

        cls.owned = make_draft_usecase(cls.owner, slug="sec-owned-en", title="Owned Usecase")
        cls.foreign = make_draft_usecase(
            cls.other_author, slug="sec-foreign-en", title="Foreign Usecase"
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

    def test_author_may_preview_their_own_usecase(self):
        self.client.force_login(self.owner)
        resp = self.client.get(preview_url(self.owned.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Owned Usecase")

    def test_author_may_not_preview_someone_elses_usecase(self):
        self.client.force_login(self.owner)
        resp = self.client.get(preview_url(self.foreign.pk))
        self.assertEqual(resp.status_code, 404)

    def test_denied_response_does_not_disclose_the_draft(self):
        self.client.force_login(self.owner)
        resp = self.client.get(preview_url(self.foreign.pk))
        self.assertNotContains(resp, "Foreign Usecase", status_code=404)

    def test_editor_admin_and_superuser_may_preview_any_usecase(self):
        for label, user in (
            ("editor", self.editor),
            ("admin-group", self.admin_group),
            ("superuser", self.superuser),
        ):
            for usecase_label, usecase in (("owned", self.owned), ("foreign", self.foreign)):
                with self.subTest(role=label, usecase=usecase_label):
                    self.client.force_login(user)
                    resp = self.client.get(preview_url(usecase.pk))
                    self.assertEqual(resp.status_code, 200)
            self.client.logout()

    def test_guessing_a_neighbouring_id_does_not_leak_a_foreign_draft(self):
        """
        IDOR guard: the object-level check runs server-side, so a non-owning
        author gets nothing but 404s - for the foreign draft's real id, for
        the immediate neighbourhood of both drafts, for ``0`` and for an id
        proven to be above every existing row.

        Beta 11.12D2 replaced the original ``range(1, foreign.pk + 2)`` walk
        with this bounded probe set. Primary keys come from a sequence that is
        never reset between test classes, so inside the full suite that loop
        issued one admin request per id the whole run had ever consumed -
        thousands of them - while the ids in between belong to unrelated
        fixtures and prove nothing this set does not. The probes below are
        additionally asserted not to disclose the foreign draft's content,
        which the original loop never checked.
        """
        probes = build_bounded_idor_probe_ids(
            own_id=self.owned.pk,
            foreign_id=self.foreign.pk,
            existing_ids=tuple(UseCase.objects.values_list("pk", flat=True)),
        )
        self.assertIn(self.foreign.pk, probes)
        self.assertNotIn(self.owned.pk, probes)
        # The highest probe is meant to hit nothing at all - proven, not assumed.
        self.assertFalse(UseCase.objects.filter(pk=max(probes)).exists())

        self.client.force_login(self.owner)
        for pk in probes:
            with self.subTest(pk=pk):
                resp = self.client.get(preview_url(pk))
                self.assertEqual(resp.status_code, 404)
                self.assertNotContains(resp, "Foreign Usecase", status_code=404)
