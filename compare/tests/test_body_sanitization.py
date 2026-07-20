"""
Beta 9.4: comparison_detail.html rendered `obj.body` via the bare `|safe`
filter, unlike every other TinyMCE-authored field on this model (`intro`)
and every other editorial detail page, which all sanitize through the
`richtext` filter before `|safe`. Both `intro` and `body` are TinyMCE
fields (see ComparisonAdmin.tinymce_fields), so `body` skipping
sanitization was an inconsistency, not a deliberate choice. This locks in
the fix: `body` must be bleach-sanitized like every other rich field.
"""
from django.test import TestCase
from django.utils import timezone

from compare.models import Comparison


class ComparisonBodySanitizationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.unsafe_marker = "UNSAFE-SCRIPT-MARKER"
        cls.safe_marker = "SAFE-STRONG-MARKER"
        cls.comparison = Comparison.objects.create(
            status="published", published_at=timezone.now()
        )
        cls.comparison.create_translation(
            "en",
            title="Sanitization Check",
            intro="i",
            body=(
                f'<script>alert("{cls.unsafe_marker}")</script>'
                f"<p><strong>{cls.safe_marker}</strong></p>"
            ),
            slug="sanitization-check-en",
        )

    def test_body_script_tag_is_stripped(self):
        resp = self.client.get("/en/compare/sanitization-check-en/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        # bleach (strip=True) removes the disallowed <script> element but
        # keeps its inert text content, exactly like every other richtext
        # field on this site - so the marker text may still appear, it
        # just must never be wrapped in an actual <script> tag again.
        self.assertNotIn("<script>alert(", html)

    def test_body_allowed_markup_still_renders(self):
        resp = self.client.get("/en/compare/sanitization-check-en/")
        html = resp.content.decode()
        self.assertIn(self.safe_marker, html)
        self.assertIn("<strong>", html)
