import io
import os
import tempfile
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from PIL import Image

User = get_user_model()


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), (0, 128, 255)).save(buf, format="PNG")
    return buf.getvalue()


class TinymceUploadPermissionTests(TestCase):
    """@staff_member_required delegates entirely to Django admin's own
    auth gate; these tests only pin the observable redirect contract."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username="staff", password="pass", is_staff=True
        )
        cls.regular = User.objects.create_user(
            username="regular", password="pass", is_staff=False
        )

    def test_anonymous_get_is_redirected_to_admin_login(self):
        resp = self.client.get(reverse("tinymce_upload"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin/login/", resp.url)

    def test_non_staff_user_is_redirected_to_admin_login(self):
        self.client.login(username="regular", password="pass")
        resp = self.client.post(
            reverse("tinymce_upload"),
            {"file": SimpleUploadedFile("a.png", b"data")},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin/login/", resp.url)

    def test_anonymous_get_image_list_is_redirected(self):
        resp = self.client.get(reverse("tinymce_image_list"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin/login/", resp.url)


class TinymceUploadStaffTests(TestCase):
    """Exercises the real save contract against a temporary FileSystemStorage
    (the storage boundary) so path-traversal handling reflects Django's real
    Storage.save(), not a mocked stand-in."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username="staff", password="pass", is_staff=True
        )

    def setUp(self):
        self.client.login(username="staff", password="pass")
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        storage = FileSystemStorage(location=self._tmpdir.name, base_url="/media/")
        patcher = mock.patch("content.views.uploads.default_storage", storage)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_get_is_rejected_as_invalid_request(self):
        resp = self.client.get(reverse("tinymce_upload"))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json(), {"error": "Invalid request"})

    def test_post_without_file_is_rejected_as_invalid_request(self):
        resp = self.client.post(reverse("tinymce_upload"), {})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json(), {"error": "Invalid request"})

    def test_valid_upload_saves_under_tinymce_and_returns_location(self):
        # Beta 11.1: request.FILES["file"] must now be a real, Pillow-
        # decodable image - see content/tests/test_upload_security.py for
        # the full content-validation matrix this hardening added.
        upload = SimpleUploadedFile("photo.png", _png_bytes(), content_type="image/png")
        resp = self.client.post(reverse("tinymce_upload"), {"file": upload})

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("location", data)

        tinymce_dir = os.path.join(self._tmpdir.name, "tinymce")
        saved_files = os.listdir(tinymce_dir)
        self.assertEqual(len(saved_files), 1)
        self.assertTrue(saved_files[0].endswith(".png"))
        self.assertIn(saved_files[0], data["location"])

    def test_upload_filename_gets_unique_server_generated_name(self):
        # Beta 11.1: the stored name is now fully server-generated (uuid4
        # hex + the Pillow-detected extension) rather than
        # f"{uuid4}_{original_name}" - see
        # content/tests/test_upload_security.py::SafeFilenameTests for why
        # (no path/unicode/original-name component is ever carried over).
        upload_a = SimpleUploadedFile("same.png", _png_bytes(), content_type="image/png")
        upload_b = SimpleUploadedFile("same.png", _png_bytes(), content_type="image/png")

        self.client.post(reverse("tinymce_upload"), {"file": upload_a})
        self.client.post(reverse("tinymce_upload"), {"file": upload_b})

        saved_files = os.listdir(os.path.join(self._tmpdir.name, "tinymce"))
        self.assertEqual(len(saved_files), 2)
        self.assertNotEqual(saved_files[0], saved_files[1])
        for name in saved_files:
            self.assertTrue(name.endswith(".png"))
            self.assertNotIn("same", name)

    def test_path_traversal_filename_cannot_escape_tinymce_directory(self):
        # Beta 11.1: the stored filename is fully server-generated (uuid4
        # hex + detected extension), so the original name - path
        # components included - is never used at all, not just sanitized.
        upload = SimpleUploadedFile("../../evil.png", _png_bytes(), content_type="image/png")
        resp = self.client.post(reverse("tinymce_upload"), {"file": upload})

        self.assertEqual(resp.status_code, 200)
        written = []
        for root, _dirs, files in os.walk(self._tmpdir.name):
            for f in files:
                written.append(os.path.join(root, f))
        self.assertEqual(len(written), 1)
        self.assertEqual(os.path.dirname(written[0]), os.path.join(self._tmpdir.name, "tinymce"))
        self.assertNotIn("evil", os.path.basename(written[0]))

    def test_response_never_leaks_the_local_filesystem_path(self):
        upload = SimpleUploadedFile("photo.png", _png_bytes(), content_type="image/png")
        resp = self.client.post(reverse("tinymce_upload"), {"file": upload})
        data = resp.json()
        self.assertNotIn(self._tmpdir.name, data["location"])


class TinymceImageListTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username="staff", password="pass", is_staff=True
        )

    def setUp(self):
        self.client.login(username="staff", password="pass")

    def test_get_returns_empty_list_when_no_images(self):
        with mock.patch("content.views.uploads.FilerImage.objects.order_by") as order_by:
            order_by.return_value = []
            resp = self.client.get(reverse("tinymce_image_list"))

        order_by.assert_called_once_with("-modified_at")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_title_falls_back_to_original_filename_when_label_is_blank(self):
        images = [
            SimpleNamespace(label="Nice title", original_filename="a.png", url="/media/a.png"),
            SimpleNamespace(label="", original_filename="b.png", url="/media/b.png"),
            SimpleNamespace(label=None, original_filename="c.png", url="/media/c.png"),
        ]
        with mock.patch("content.views.uploads.FilerImage.objects.order_by") as order_by:
            order_by.return_value = images
            resp = self.client.get(reverse("tinymce_image_list"))

        self.assertEqual(
            resp.json(),
            [
                {"title": "Nice title", "value": "/media/a.png"},
                {"title": "b.png", "value": "/media/b.png"},
                {"title": "c.png", "value": "/media/c.png"},
            ],
        )

    def test_result_is_capped_at_200_items(self):
        images = [
            SimpleNamespace(label=f"img{i}", original_filename=f"{i}.png", url=f"/media/{i}.png")
            for i in range(205)
        ]
        with mock.patch("content.views.uploads.FilerImage.objects.order_by") as order_by:
            order_by.return_value = images
            resp = self.client.get(reverse("tinymce_image_list"))

        self.assertEqual(len(resp.json()), 200)
