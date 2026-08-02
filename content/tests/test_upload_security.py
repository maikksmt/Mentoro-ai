"""
Beta 11.1: verifies and locks in the fix for content.views.uploads.tinymce_upload
accepting any file content regardless of declared MIME type or extension.

Confirmed pre-fix contract: the endpoint only checked the HTTP method and
that a "file" key was present in request.FILES - request.FILES["file"] was
written to storage as-is, under a name of `f"{uuid4().hex}_{f.name}"`. There
was no size limit, no content inspection, and no format whitelist: a
text file, an HTML file, or an SVG file uploaded with any extension (real or
spoofed) was written to MEDIA_ROOT/tinymce/ and served back through
default_storage.url() exactly like a genuine image.

This module exercises the real URL endpoint (reverse("tinymce_upload"))
against a temporary FileSystemStorage, the same storage boundary the
existing content/tests/test_uploads.py permission/contract tests use, so
path handling reflects Django's actual Storage.save() rather than a mock.
"""
import io
import os
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from PIL import Image

User = get_user_model()


def _image_bytes(fmt: str, size=(20, 20), color=(255, 0, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format=fmt)
    return buf.getvalue()


PNG_BYTES = _image_bytes("PNG")
JPEG_BYTES = _image_bytes("JPEG")
WEBP_BYTES = _image_bytes("WEBP")


class UploadSecurityTestsBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="upload-staff", password="pass", is_staff=True)

    def setUp(self):
        self.client.login(username="upload-staff", password="pass")
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        storage = FileSystemStorage(location=self._tmpdir.name, base_url="/media/")
        patcher = mock.patch("content.views.uploads.default_storage", storage)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _post(self, upload):
        return self.client.post(reverse("tinymce_upload"), {"file": upload})

    def _all_written_files(self):
        written = []
        for root, _dirs, files in os.walk(self._tmpdir.name):
            for f in files:
                written.append(os.path.join(root, f))
        return written


class AllowedImageFormatTests(UploadSecurityTestsBase):
    def test_valid_png_is_accepted(self):
        resp = self._post(SimpleUploadedFile("photo.png", PNG_BYTES, content_type="image/png"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("location", resp.json())
        self.assertEqual(len(self._all_written_files()), 1)

    def test_valid_jpeg_is_accepted(self):
        resp = self._post(SimpleUploadedFile("photo.jpg", JPEG_BYTES, content_type="image/jpeg"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("location", resp.json())

    def test_valid_webp_is_accepted(self):
        resp = self._post(SimpleUploadedFile("photo.webp", WEBP_BYTES, content_type="image/webp"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("location", resp.json())


class RejectedFileTests(UploadSecurityTestsBase):
    def _assert_rejected(self, resp):
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.json())
        self.assertEqual(self._all_written_files(), [])

    def test_plain_text_file_is_rejected(self):
        resp = self._post(SimpleUploadedFile("notes.txt", b"just some text", content_type="text/plain"))
        self._assert_rejected(resp)

    def test_text_file_disguised_with_png_extension_is_rejected(self):
        resp = self._post(SimpleUploadedFile("fake.png", b"not actually a png", content_type="image/png"))
        self._assert_rejected(resp)

    def test_html_file_is_rejected(self):
        payload = b"<html><body><script>alert(1)</script></body></html>"
        resp = self._post(SimpleUploadedFile("page.html", payload, content_type="text/html"))
        self._assert_rejected(resp)

    def test_svg_file_is_rejected(self):
        payload = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        resp = self._post(SimpleUploadedFile("vector.svg", payload, content_type="image/svg+xml"))
        self._assert_rejected(resp)

    def test_empty_file_is_rejected(self):
        resp = self._post(SimpleUploadedFile("empty.png", b"", content_type="image/png"))
        self._assert_rejected(resp)

    def test_truncated_corrupted_image_is_rejected(self):
        corrupted = PNG_BYTES[: len(PNG_BYTES) // 2]
        resp = self._post(SimpleUploadedFile("broken.png", corrupted, content_type="image/png"))
        self._assert_rejected(resp)

    def test_oversized_file_is_rejected(self):
        from content.views import uploads as uploads_module

        with mock.patch.object(uploads_module, "TINYMCE_UPLOAD_MAX_BYTES", 100):
            big = _image_bytes("PNG", size=(200, 200))
            self.assertGreater(len(big), 100)
            resp = self._post(SimpleUploadedFile("big.png", big, content_type="image/png"))
        self._assert_rejected(resp)

    def test_decompression_bomb_is_rejected_not_a_500(self):
        with mock.patch("PIL.Image.MAX_IMAGE_PIXELS", 10):
            resp = self._post(SimpleUploadedFile("bomb.png", PNG_BYTES, content_type="image/png"))
        self._assert_rejected(resp)

    def test_error_response_never_leaks_pillow_or_filesystem_details(self):
        resp = self._post(SimpleUploadedFile("notes.txt", b"just some text", content_type="text/plain"))
        body = resp.content.decode()
        self.assertNotIn(self._tmpdir.name, body)
        self.assertNotIn("PIL", body)
        self.assertNotIn("Traceback", body)


class SafeFilenameTests(UploadSecurityTestsBase):
    def test_filename_with_path_components_cannot_escape_tinymce_directory(self):
        upload = SimpleUploadedFile("../../evil.png", PNG_BYTES, content_type="image/png")
        resp = self._post(upload)
        self.assertEqual(resp.status_code, 200)
        written = self._all_written_files()
        self.assertEqual(len(written), 1)
        self.assertEqual(os.path.dirname(written[0]), os.path.join(self._tmpdir.name, "tinymce"))

    def test_filename_with_unicode_and_special_characters_is_accepted_safely(self):
        upload = SimpleUploadedFile("fotö äöü ½ (1).png", PNG_BYTES, content_type="image/png")
        resp = self._post(upload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn("ö", data["location"])
        self.assertNotIn(" ", data["location"])

    def test_stored_filename_never_contains_the_original_name(self):
        upload = SimpleUploadedFile("my-original-name.png", PNG_BYTES, content_type="image/png")
        resp = self._post(upload)
        data = resp.json()
        self.assertNotIn("my-original-name", data["location"])

    def test_duplicate_original_filenames_do_not_collide(self):
        self._post(SimpleUploadedFile("same.png", PNG_BYTES, content_type="image/png"))
        self._post(SimpleUploadedFile("same.png", PNG_BYTES, content_type="image/png"))
        written = self._all_written_files()
        self.assertEqual(len(written), 2)
        self.assertNotEqual(os.path.basename(written[0]), os.path.basename(written[1]))

    def test_stored_extension_matches_detected_format_not_original_extension(self):
        # .png extension, real JPEG content - the stored file must be named
        # after the format Pillow actually detected, never trust the name.
        upload = SimpleUploadedFile("mislabeled.png", JPEG_BYTES, content_type="image/png")
        resp = self._post(upload)
        self.assertEqual(resp.status_code, 200)
        written = self._all_written_files()
        self.assertTrue(written[0].endswith(".jpg"))
