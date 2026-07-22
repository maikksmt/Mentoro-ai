"""
Beta 11.3: the editor content-style architecture.

The editor iframe shares one compiled source with the public site
(static/css/output.css) and adds only genuine editor chrome via
static/css/tinymce-editor.css. These tests assert that the editor-chrome
file no longer carries the pre-11.3 hardcodings that broke parity (forced
black text/links, forced white background, forced full width, foreign
line-height) and that it does not re-define content typography - without
pinning the exact compiled CSS (no golden master, no hashes).

The editor-chrome file is intentionally checked with its CSS comments
stripped: the file documents the removed hardcodings in prose (using their
literal syntax), and the contract is about the effective CSS rules, not the
explanatory comments.
"""
import re
from pathlib import Path

from django.conf import settings
from django.contrib.staticfiles import finders
from django.test import SimpleTestCase

_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _read_static(rel_path):
    found = finders.find(rel_path)
    assert found, f"{rel_path} not found via staticfiles finders"
    return Path(found).read_text(encoding="utf-8")


def _strip_comments(css):
    return _CSS_COMMENT.sub("", css)


class EditorChromeFileTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Effective rules only - comments (which describe the removed
        # hardcodings verbatim) are stripped so they don't trip the guards.
        cls.rules = _strip_comments(_read_static("css/tinymce-editor.css"))
        cls.rules_lower = cls.rules.lower()

    def test_no_forced_black_text_hardcoding(self):
        # The pre-11.3 file forced black on the body, headings, links and
        # markers. Content color must now come from the shared prose/theme
        # tokens instead.
        self.assertNotIn("#000000", self.rules)
        self.assertNotIn("#000", self.rules)

    def test_no_forced_white_background_hardcoding(self):
        self.assertNotIn("#ffffff", self.rules)
        self.assertNotIn("#fff", self.rules)

    def test_no_forced_full_width_override(self):
        self.assertNotIn("max-width:100%", self.rules_lower.replace(" ", ""))

    def test_no_important_declarations_remain(self):
        # All remaining rules are plain editor chrome that must never fight
        # the shared content stylesheet.
        self.assertNotIn("!important", self.rules)

    def test_no_foreign_content_line_height(self):
        self.assertNotIn("line-height", self.rules_lower)

    def test_does_not_force_a_fixed_color_scheme(self):
        # `color-scheme: only light` blocked a future dark-theme sync; the
        # shared theme (output.css :where(:root)) already sets the scheme.
        self.assertNotIn("color-scheme", self.rules_lower)

    def test_canvas_background_uses_shared_theme_token(self):
        self.assertIn("var(--color-base-100)", self.rules)

    def test_does_not_redefine_content_typography(self):
        # No second definition of headings/link/marker styling in the editor
        # chrome file - those belong to the shared output.css only.
        for needle in ("h2", "h3", "h4", "::marker", "blockquote"):
            self.assertNotIn(needle, self.rules_lower)

    def test_body_font_family_restores_the_shared_token_not_a_new_value(self):
        # Confirmed only in the real, django-tinymce-initialized iframe (not
        # a plain-HTML harness): TinyMCE's own oxide skin
        # (static/tinymce/skins/ui/oxide/content.min.css, loaded before this
        # file) ships an UNLAYERED `body { font-family: sans-serif }` rule.
        # Per the CSS Cascade Layers spec, any unlayered rule beats ANY
        # layered rule for the same element/property regardless of load
        # order or specificity, so output.css's own `body { font-family:
        # var(--font-sans) }` (inside `@layer base`) lost to it - paragraph
        # text rendered in a generic sans-serif fallback instead of Inter.
        # This restates the exact same shared token, unlayered, so it wins
        # against oxide's unlayered rule - not a new or diverging value.
        self.assertIn("body", self.rules_lower)
        self.assertIn("font-family: var(--font-sans)", self.rules)
        self.assertNotIn("font-family: sans-serif", self.rules_lower)
        self.assertNotIn('font-family: "inter"', self.rules_lower)


class SharedStylesheetTests(SimpleTestCase):
    def test_content_css_points_at_the_single_compiled_public_source(self):
        self.assertIn(
            "/static/css/output.css", settings.TINYMCE_DEFAULT_CONFIG["content_css"]
        )

    def test_compiled_public_source_defines_the_reading_column_measure(self):
        # The parity-critical rule the editor now inherits via body_class.
        output_css = _read_static("css/output.css")
        self.assertIn("reading-column", output_css)
        self.assertIn("70ch", output_css)
