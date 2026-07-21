from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase

User = get_user_model()


class EditorialGroupsTests(TestCase):
    def test_groups_created(self):
        for group_name in ("Admin", "Editor", "Author"):
            with self.subTest(group=group_name):
                self.assertTrue(Group.objects.filter(name=group_name).exists())

    def test_author_group_has_no_delete_permissions(self):
        author_group = Group.objects.get(name="Author")
        self.assertFalse(
            author_group.permissions.filter(codename__startswith="delete_").exists()
        )

    def test_editor_group_has_no_blanket_delete_permissions(self):
        """
        Since accounts/signals.py's Editor group definition dropped "delete"
        from its prefixes (commit 14a31a8, "centralize admin object
        permissions and setup permissions for objects and Roles"), deletion
        is no longer a blanket per-app Django permission for Editor - it's
        the object-level content.archive rule below.
        """
        editor_group = Group.objects.get(name="Editor")
        self.assertFalse(
            editor_group.permissions.filter(codename__startswith="delete_").exists()
        )

    def test_admin_group_has_global_permission_scope(self):
        admin_group = Group.objects.get(name="Admin")
        self.assertTrue(admin_group.permissions.filter(codename="delete_user").exists())


class ContentArchivePermissionTests(TestCase):
    """
    The real "delete" contract for editorial content is core/authz.py's
    content.archive rule (is_author | is_editor), checked object-by-object
    via rules.permissions.ObjectPermissionBackend (see
    AUTHENTICATION_BACKENDS) rather than via a group-wide delete_*
    permission - see EditorialGroupsTests.test_editor_group_has_no_blanket_delete_permissions.
    """

    @classmethod
    def setUpTestData(cls):
        cls.editor = User.objects.create_user(username="editor", password="pass")
        cls.editor.groups.add(Group.objects.get(name="Editor"))

        cls.author = User.objects.create_user(username="author", password="pass")

        cls.bystander = User.objects.create_user(username="bystander", password="pass")

    def test_author_can_archive_their_own_content(self):
        own_content = SimpleNamespace(author_id=self.author.pk)
        self.assertTrue(self.author.has_perm("content.archive", own_content))

    def test_author_cannot_archive_someone_elses_content(self):
        others_content = SimpleNamespace(author_id=self.editor.pk)
        self.assertFalse(self.author.has_perm("content.archive", others_content))

    def test_editor_can_archive_content_that_is_not_their_own(self):
        someone_elses_content = SimpleNamespace(author_id=self.author.pk)
        self.assertTrue(self.editor.has_perm("content.archive", someone_elses_content))

    def test_bystander_without_a_role_cannot_archive_others_content(self):
        someone_elses_content = SimpleNamespace(author_id=self.author.pk)
        self.assertFalse(self.bystander.has_perm("content.archive", someone_elses_content))
