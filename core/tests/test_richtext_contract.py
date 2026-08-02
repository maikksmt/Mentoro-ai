"""
Beta 11.2: the canonical rich-text contract and renderer.

These tests pin the *exact* public sanitization contract that Beta 11.2
extracted, unchanged, from ``content.templatetags.richtext`` into
:mod:`core.richtext`. Every expected value here was captured from the
pre-refactor filter's real output, so the suite doubles as a golden-master
guard: the renderer must keep producing byte-identical HTML.

Two current behaviours are load-bearing and deliberately asserted as-is
(both flagged for a later, separate hardening slice - never changed here):

* ``data-*`` / ``aria-*`` attributes are *stripped* (bleach treats the
  ``"data-*"`` / ``"aria-*"`` entries as literal attribute names, not
  globs).
* ``data:`` is an allowed URL protocol, so ``<img src="data:...">`` is kept.
"""
import dataclasses
from typing import ClassVar

from django.test import SimpleTestCase
from django.utils.safestring import SafeString, mark_safe

from core.richtext import RICHTEXT_CONTRACT, render_content
from core.richtext.contract import (
    ALLOWED_ATTRIBUTES,
    ALLOWED_CSS_PROPERTIES,
    ALLOWED_PROTOCOLS,
    ALLOWED_TAGS,
)


class AllowedTagContractTests(SimpleTestCase):
    """Every tag in the allow-list survives rendering in a representative,
    validly-nested snippet."""

    # tag -> (input, opening-tag substring that must survive)
    TAG_SNIPPETS: ClassVar[dict[str, tuple[str, str]]] = {
        "p": ("<p>x</p>", "<p"),
        "br": ("<p>a<br>b</p>", "<br"),
        "hr": ("<hr>", "<hr"),
        "blockquote": ("<blockquote>x</blockquote>", "<blockquote"),
        "pre": ("<pre>x</pre>", "<pre"),
        "code": ("<code>x</code>", "<code"),
        "span": ("<span>x</span>", "<span"),
        "h2": ("<h2>x</h2>", "<h2"),
        "h3": ("<h3>x</h3>", "<h3"),
        "h4": ("<h4>x</h4>", "<h4"),
        "h5": ("<h5>x</h5>", "<h5"),
        "ul": ("<ul><li>x</li></ul>", "<ul"),
        "ol": ("<ol><li>x</li></ol>", "<ol"),
        "li": ("<ul><li>x</li></ul>", "<li"),
        "strong": ("<strong>x</strong>", "<strong"),
        "em": ("<em>x</em>", "<em"),
        "u": ("<u>x</u>", "<u"),
        "s": ("<s>x</s>", "<s"),
        "sub": ("<sub>x</sub>", "<sub"),
        "sup": ("<sup>x</sup>", "<sup"),
        "b": ("<b>x</b>", "<b"),
        "i": ("<i>x</i>", "<i"),
        "a": ('<a href="https://e.com">x</a>', "<a"),
        "img": ('<img src="https://e.com/a.png" alt="a">', "<img"),
        "figure": ("<figure><figcaption>c</figcaption></figure>", "<figure"),
        "figcaption": ("<figure><figcaption>c</figcaption></figure>", "<figcaption"),
        "table": ("<table><tbody><tr><td>d</td></tr></tbody></table>", "<table"),
        "thead": (
            "<table><thead><tr><th>h</th></tr></thead></table>",
            "<thead",
        ),
        "tbody": ("<table><tbody><tr><td>d</td></tr></tbody></table>", "<tbody"),
        "tr": ("<table><tbody><tr><td>d</td></tr></tbody></table>", "<tr"),
        "th": ("<table><thead><tr><th>h</th></tr></thead></table>", "<th"),
        "td": ("<table><tbody><tr><td>d</td></tr></tbody></table>", "<td"),
        "div": ("<div>x</div>", "<div"),
        "section": ("<section>x</section>", "<section"),
        "article": ("<article>x</article>", "<article"),
        "header": ("<header>x</header>", "<header"),
        "footer": ("<footer>x</footer>", "<footer"),
        "nav": ("<nav>x</nav>", "<nav"),
    }

    def test_every_allowed_tag_has_a_representative_case(self):
        self.assertEqual(set(self.TAG_SNIPPETS), set(ALLOWED_TAGS))

    def test_allowed_tags_survive_rendering(self):
        for tag, (html, expected_open) in self.TAG_SNIPPETS.items():
            with self.subTest(tag=tag):
                out = render_content(html)
                self.assertIn(expected_open, out)


class ForbiddenTagContractTests(SimpleTestCase):
    """Tags outside the allow-list are stripped as elements (their inert
    text content may remain, exactly as ``strip=True`` has always done)."""

    # tag -> input using that tag; the element must not survive
    FORBIDDEN: ClassVar[dict[str, str]] = {
        "script": "<script>alert(1)</script>keep",
        "style": "<style>.x{color:red}</style>keep",
        "iframe": '<iframe src="https://e.com"></iframe>keep',
        "object": "<object>keep</object>",
        "embed": '<embed src="x">keep',
        "form": "<form>keep</form>",
        "input": '<input value="x">keep',
        "button": "<button>keep</button>",
        "h1": "<h1>keep</h1>",
        "caption": "<table><caption>keep</caption><tbody><tr><td>d</td></tr></tbody></table>",
        "colgroup": "<table><colgroup><col></colgroup><tbody><tr><td>d</td></tr></tbody></table>",
        "marquee": "<marquee>keep</marquee>",
    }

    def test_forbidden_tags_are_stripped(self):
        for tag, html in self.FORBIDDEN.items():
            with self.subTest(tag=tag):
                out = render_content(html)
                self.assertNotIn(f"<{tag}", out)

    def test_script_content_becomes_inert_text_exactly_as_before(self):
        # strip=True keeps the disallowed element's text, but never the tag.
        self.assertEqual(render_content("<script>alert(1)</script>x"), "alert(1)x")


class AllowedAttributeContractTests(SimpleTestCase):
    """Representative per-tag allowed attributes survive."""

    def test_global_attributes_survive(self):
        out = render_content('<p class="c" id="i" style="color:red">x</p>')
        self.assertIn('class="c"', out)
        self.assertIn('id="i"', out)
        self.assertIn("color:red", out)

    def test_anchor_attributes_survive(self):
        out = render_content(
            '<a href="https://e.com" title="t" rel="nofollow" target="_blank">x</a>'
        )
        for token in ('href="https://e.com"', 'title="t"', 'rel="nofollow"', 'target="_blank"'):
            self.assertIn(token, out)

    def test_image_attributes_survive(self):
        out = render_content(
            '<img src="https://e.com/a.png" alt="a" title="t" width="10" height="20" loading="lazy">'
        )
        for token in ('src="https://e.com/a.png"', 'alt="a"', 'title="t"',
                      'width="10"', 'height="20"', 'loading="lazy"'):
            self.assertIn(token, out)

    def test_table_and_cell_attributes_survive(self):
        out = render_content(
            '<table border="1" cellpadding="2" cellspacing="0">'
            "<tbody><tr><td colspan=\"2\" rowspan=\"3\">d</td></tr></tbody></table>"
        )
        for token in ('border="1"', 'cellpadding="2"', 'cellspacing="0"',
                      'colspan="2"', 'rowspan="3"'):
            self.assertIn(token, out)


class ForbiddenAttributeContractTests(SimpleTestCase):
    """Event handlers, unknown attributes, and (per the documented baseline)
    data-*/aria-* are removed."""

    def test_event_handler_is_removed(self):
        out = render_content('<img src="x" onerror="alert(1)">')
        self.assertNotIn("onerror", out)
        self.assertEqual(out, '<img src="x">')

    def test_unknown_attribute_is_removed(self):
        self.assertEqual(render_content('<p foo="bar">x</p>'), "<p>x</p>")

    def test_data_and_aria_attributes_are_stripped_baseline(self):
        # Documented current behaviour: "data-*"/"aria-*" are literal names
        # to bleach, not globs, so concrete data-*/aria-* attributes are
        # stripped. Preserved unchanged in this slice; flagged for later.
        self.assertEqual(
            render_content('<div data-x="1" aria-label="y" class="z">t</div>'),
            '<div class="z">t</div>',
        )


class ProtocolContractTests(SimpleTestCase):
    """Allowed protocols pass; others have their URL attribute dropped."""

    def test_allowed_link_protocols_survive(self):
        self.assertIn('href="https://e.com"', render_content('<a href="https://e.com">x</a>'))
        self.assertIn('href="http://e.com"', render_content('<a href="http://e.com">x</a>'))
        self.assertIn('href="mailto:a@b.co"', render_content('<a href="mailto:a@b.co">x</a>'))

    def test_data_protocol_on_image_is_kept_baseline(self):
        # Documented baseline: "data" is an allowed protocol today. Kept
        # unchanged in this slice; flagged for later hardening.
        out = render_content('<img src="data:image/png;base64,AAAA" alt="a">')
        self.assertIn('src="data:image/png;base64,AAAA"', out)

    def test_javascript_protocol_is_stripped(self):
        self.assertEqual(render_content('<a href="javascript:alert(1)">x</a>'), "<a>x</a>")


class CssPropertyContractTests(SimpleTestCase):
    """Allowed inline-CSS properties survive (the sanitizer appends a
    trailing ';'); forbidden ones are dropped."""

    def test_allowed_css_properties_survive(self):
        out = render_content('<p style="color:red;margin-top:4px">x</p>')
        self.assertEqual(out, '<p style="color:red;margin-top:4px;">x</p>')

    def test_forbidden_css_property_is_removed(self):
        out = render_content('<p style="position:absolute">x</p>')
        self.assertEqual(out, '<p style="">x</p>')

    def test_forbidden_css_property_mixed_with_allowed(self):
        out = render_content('<p style="position:absolute;color:blue">x</p>')
        self.assertIn("color:blue", out)
        self.assertNotIn("position", out)


class RendererValueContractTests(SimpleTestCase):
    """None / empty / text / broken HTML / unicode / entities / SafeString."""

    def test_none_returns_empty_safestring(self):
        out = render_content(None)
        self.assertEqual(out, "")
        self.assertIsInstance(out, SafeString)

    def test_empty_string_returns_empty_safestring(self):
        out = render_content("")
        self.assertEqual(out, "")
        self.assertIsInstance(out, SafeString)

    def test_falsy_non_string_returns_empty(self):
        # Mirrors the previous filter's ``if not html`` short-circuit.
        self.assertEqual(render_content(0), "")

    def test_plain_text_passes_through(self):
        self.assertEqual(render_content("just text"), "just text")

    def test_entities_are_not_double_escaped(self):
        self.assertEqual(render_content("a &amp; b &lt;c&gt;"), "a &amp; b &lt;c&gt;")

    def test_unicode_and_german_characters_preserved(self):
        self.assertEqual(render_content("Grüße — Straße €"), "Grüße — Straße €")

    def test_broken_html_does_not_raise_and_is_repaired(self):
        self.assertEqual(render_content("<p>unclosed <b>bold"), "<p>unclosed <b>bold</b></p>")

    def test_html_comment_is_removed(self):
        self.assertEqual(render_content("a<!-- c -->b"), "ab")

    def test_safestring_input_is_still_sanitized(self):
        # A SafeString carrying a disallowed element must still be cleaned.
        out = render_content(mark_safe("<script>alert(1)</script><b>x</b>"))
        self.assertNotIn("<script", out)
        self.assertIn("<b>x</b>", out)

    def test_output_is_deterministic(self):
        html = '<p style="color:red">x</p><a href="https://e.com">l</a>'
        self.assertEqual(render_content(html), render_content(html))


class ReturnTypeContractTests(SimpleTestCase):
    """Only the sanitized result is marked safe; forbidden content is gone
    from the underlying string, not merely escaped."""

    def test_output_is_safestring(self):
        self.assertIsInstance(render_content("<p>x</p>"), SafeString)

    def test_forbidden_content_absent_from_underlying_string(self):
        raw = str(render_content('<img src="x" onerror="alert(1)"><script>alert(2)</script>'))
        self.assertNotIn("onerror", raw)
        self.assertNotIn("<script", raw)


class IdempotencyContractTests(SimpleTestCase):
    """render_content(render_content(v)) == render_content(v)."""

    FIXTURES: ClassVar[dict[str, str]] = {
        "plain": "plain text no markup",
        "paragraph": "<p>Hello world</p>",
        "headings_list": "<h2>H</h2><ul><li>a</li></ul>",
        "dangerous": "<script>alert(1)</script><p>keep</p>",
        "event_handler": "<img src=x onerror=alert(1)>",
        "table": "<table><caption>c</caption><colgroup><col></colgroup><tbody><tr><td>1</td></tr></tbody></table>",
        "link": '<a href="https://e.com" title="t" rel="nofollow" target="_blank">l</a>',
        "image": '<img src="https://e.com/a.png" alt="a">',
        "inline_styles": '<p style="color:red;margin-top:4px">styled</p>',
        "nested": '<section><div class="box"><p>nested <strong>x</strong></p></div></section>',
        "entities": "a &amp; b &lt;c&gt;",
        "data_protocol": '<img src="data:image/png;base64,AAAA" alt="a">',
    }

    def test_rendering_is_idempotent(self):
        for name, html in self.FIXTURES.items():
            with self.subTest(fixture=name):
                once = render_content(html)
                twice = render_content(once)
                self.assertEqual(once, twice)


class GoldenMasterTests(SimpleTestCase):
    """Byte-for-byte output of representative content components, captured
    from the pre-Beta-11.2 filter. Any drift here is a public-rendering
    change and must fail."""

    GOLDEN: ClassVar[dict[str, tuple[str, str]]] = {
        "paragraph": ("<p>Hello world</p>", "<p>Hello world</p>"),
        "h2": ("<h2>Heading two</h2>", "<h2>Heading two</h2>"),
        "h3": ("<h3>Heading three</h3>", "<h3>Heading three</h3>"),
        "h4": ("<h4>Heading four</h4>", "<h4>Heading four</h4>"),
        "ul": ("<ul><li>one</li><li>two</li></ul>", "<ul><li>one</li><li>two</li></ul>"),
        "ol": ("<ol><li>first</li><li>second</li></ol>", "<ol><li>first</li><li>second</li></ol>"),
        "link": (
            '<a href="https://example.com" title="t" rel="nofollow" target="_blank">link</a>',
            '<a href="https://example.com" title="t" rel="nofollow" target="_blank">link</a>',
        ),
        "image": (
            '<img src="https://example.com/x.png" alt="alt" title="t" width="10" height="20" loading="lazy">',
            '<img src="https://example.com/x.png" alt="alt" title="t" width="10" height="20" loading="lazy">',
        ),
        "table": (
            (
                '<table border="1" cellpadding="2" cellspacing="0"><thead><tr><th colspan="2">H</th></tr></thead>'
                '<tbody><tr><td rowspan="2">c</td></tr></tbody></table>'
            ),
            (
                '<table border="1" cellpadding="2" cellspacing="0"><thead><tr><th colspan="2">H</th></tr></thead>'
                '<tbody><tr><td rowspan="2">c</td></tr></tbody></table>'
            ),
        ),
        "blockquote": ("<blockquote>quoted</blockquote>", "<blockquote>quoted</blockquote>"),
        "inline_code": ("<p>use <code>x=1</code> here</p>", "<p>use <code>x=1</code> here</p>"),
        "codeblock": ("<pre><code>def f():\n    pass</code></pre>", "<pre><code>def f():\n    pass</code></pre>"),
        "callout_classes": (
            '<div class="callout callout-tip" id="c1">Tip content</div>',
            '<div class="callout callout-tip" id="c1">Tip content</div>',
        ),
        "nested": (
            (
                '<section><header><h2>Title</h2></header><div class="box"><p>Para with '
                '<strong>bold</strong> and <em>em</em> and <a href="mailto:a@b.co">mail</a>.</p>'
                "<ul><li>li1</li></ul></div><footer>foot</footer></section>"
            ),
            (
                '<section><header><h2>Title</h2></header><div class="box"><p>Para with '
                '<strong>bold</strong> and <em>em</em> and <a href="mailto:a@b.co">mail</a>.</p>'
                "<ul><li>li1</li></ul></div><footer>foot</footer></section>"
            ),
        ),
    }

    def test_golden_master_output_is_stable(self):
        for name, (html, expected) in self.GOLDEN.items():
            with self.subTest(component=name):
                self.assertEqual(str(render_content(html)), expected)


class ContractStructureTests(SimpleTestCase):
    """The immutable RICHTEXT_CONTRACT mirrors the canonical primitive
    allow-lists the renderer actually uses (single source of truth)."""

    def test_contract_tags_match_allow_list(self):
        self.assertEqual(RICHTEXT_CONTRACT.tags, tuple(ALLOWED_TAGS))

    def test_contract_protocols_match_allow_list(self):
        self.assertEqual(RICHTEXT_CONTRACT.protocols, tuple(ALLOWED_PROTOCOLS))

    def test_contract_css_properties_match_allow_list(self):
        self.assertEqual(RICHTEXT_CONTRACT.css_properties, tuple(ALLOWED_CSS_PROPERTIES))

    def test_contract_attributes_match_allow_list(self):
        self.assertEqual(
            RICHTEXT_CONTRACT.attributes,
            {tag: tuple(attrs) for tag, attrs in ALLOWED_ATTRIBUTES.items()},
        )

    def test_contract_is_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            RICHTEXT_CONTRACT.tags = ()  # type: ignore[misc]
