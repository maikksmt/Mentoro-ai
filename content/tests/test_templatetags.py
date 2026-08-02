from django.template import Context, Template
from django.test import SimpleTestCase, TestCase
from django.utils.safestring import SafeString

from compare.templatetags.get_item import get_item
from content.templatetags.richtext import richtext


class TestTemplateTagsCore(TestCase):
    def render(self, tpl, ctx=None):
        return Template(tpl).render(Context(ctx or {}))

    def test_richtext_safe_render(self):
        tpl = '{% load richtext %}{{ "<b>x</b>"|richtext }}'
        out = self.render(tpl)
        self.assertIn("<b>x</b>", out)


class TestRichtextFilterAdapter(SimpleTestCase):
    """Beta 11.2: the filter is now a thin adapter over
    core.richtext.render_content. These pin the public API contract callers
    (templates and the Beta 11.1 admin display methods) rely on."""

    def test_filter_is_still_importable_from_templatetags_module(self):
        # guides/prompts/usecases/compare admin.py all do
        # `from content.templatetags.richtext import richtext`.
        from content.templatetags.richtext import richtext as imported

        self.assertTrue(callable(imported))

    def test_filter_delegates_to_canonical_renderer(self):
        from core.richtext import render_content

        html = '<script>alert(1)</script><p style="color:red">x</p><a href="javascript:evil">l</a>'
        self.assertEqual(richtext(html), render_content(html))

    def test_filter_returns_safestring(self):
        self.assertIsInstance(richtext("<b>x</b>"), SafeString)

    def test_filter_sanitizes_dangerous_markup(self):
        out = richtext('<img src="x" onerror="alert(1)">')
        self.assertNotIn("onerror", out)

    def test_filter_empty_input(self):
        self.assertEqual(richtext(""), "")
        self.assertEqual(richtext(None), "")


class TestTemplateTagGetItem(SimpleTestCase):
    def test_dict_and_missing(self):
        d = {"a": 1, "b": 0}
        self.assertEqual(get_item(d, "a"), 1)
        self.assertEqual(get_item(d, "b"), 0)
        self.assertEqual(get_item(d, "x"), "")
