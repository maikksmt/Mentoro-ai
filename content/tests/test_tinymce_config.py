"""
Beta 11.3: the TinyMCE configuration contract for editor/public content
parity.

Only visual config is touched in this slice: body_class gains
`reading-column` (so the editor iframe uses the same 70ch reading measure
as the public article.prose.reading-column surface) and the visualblocks /
visualchars plugins no longer start active. Everything that would change the
HTML schema, the toolbar, the plugin set, or the upload contract must stay
exactly as before - these tests pin both the intended changes and the
must-not-change surface.
"""
from django.conf import settings
from django.test import SimpleTestCase


class TinyMCEBodyClassTests(SimpleTestCase):
    def setUp(self):
        self.config = settings.TINYMCE_DEFAULT_CONFIG
        self.body_class = self.config["body_class"].split()

    def test_body_class_uses_shared_public_content_classes(self):
        # Matches the public richtext wrapper (prose + reading-column) so the
        # iframe resolves the same typography and 70ch measure from output.css.
        self.assertIn("prose", self.body_class)
        self.assertIn("reading-column", self.body_class)

    def test_body_class_keeps_richtext_body_marker(self):
        self.assertIn("richtext-body", self.body_class)


class TinyMCEContentCssTests(SimpleTestCase):
    def setUp(self):
        self.config = settings.TINYMCE_DEFAULT_CONFIG

    def test_content_css_loads_shared_public_stylesheet_then_editor_chrome(self):
        self.assertEqual(
            self.config["content_css"],
            ["/static/css/output.css", "/static/css/tinymce-editor.css"],
        )

    def test_shared_public_stylesheet_is_referenced_exactly_once(self):
        self.assertEqual(
            self.config["content_css"].count("/static/css/output.css"), 1
        )


class TinyMCEVisualStateTests(SimpleTestCase):
    def setUp(self):
        self.config = settings.TINYMCE_DEFAULT_CONFIG

    def test_visualblocks_does_not_start_active(self):
        self.assertIs(self.config["visualblocks_default_state"], False)

    def test_visualchars_does_not_start_active(self):
        self.assertIs(self.config["visualchars_default_state"], False)

    def test_visualblocks_and_visualchars_stay_reachable_via_plugins_and_toolbar(self):
        # The features are only turned off by default, never removed.
        self.assertIn("visualblocks", self.config["plugins"])
        self.assertIn("visualchars", self.config["plugins"])
        self.assertIn("visualblocks", self.config["toolbar"])
        self.assertIn("visualchars", self.config["toolbar"])


class TinyMCEUnchangedContractTests(SimpleTestCase):
    """Guards the surfaces this slice must NOT touch (schema, toolbar,
    plugins, upload)."""

    def setUp(self):
        self.config = settings.TINYMCE_DEFAULT_CONFIG

    def test_no_html_schema_restriction_is_introduced(self):
        for key in ("valid_elements", "extended_valid_elements",
                    "valid_classes", "valid_styles"):
            self.assertNotIn(key, self.config)

    def test_invalid_elements_still_only_blocks_h1(self):
        self.assertEqual(self.config["invalid_elements"], "h1")

    def test_plugin_set_is_unchanged(self):
        expected = (
            "anchor autolink autoresize autosave charmap code codesample "
            "emoticons fullscreen image link lists media preview quickbars "
            "searchreplace table visualblocks visualchars insertdatetime wordcount"
        )
        self.assertEqual(self.config["plugins"].split(), expected.split())

    def test_menubar_stays_disabled(self):
        self.assertIs(self.config["menubar"], False)

    def test_upload_contract_is_unchanged(self):
        self.assertIs(self.config["automatic_uploads"], True)
        self.assertIs(self.config["images_upload_credentials"], True)
        self.assertEqual(self.config["image_list"], "/admin/tinymce/image-list/")
        self.assertIn("/admin/tinymce/upload/", self.config["images_upload_handler"])

    def test_style_formats_are_unchanged(self):
        titles = [group["title"] for group in self.config["style_formats"]]
        self.assertEqual(titles, ["Headings", "Inline", "Blocks"])
