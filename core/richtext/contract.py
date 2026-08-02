"""
The single canonical definition of the public rich-text sanitization
contract for MentoroAI.

Every server-side surface that renders author-authored rich text (the
``richtext`` template filter and the Beta 11.1 readonly-admin display
methods that delegate to it) is bound by exactly this contract: the set of
allowed HTML tags, the per-tag allowed attributes, the allowed URL
protocols, and the allowed inline-CSS properties.

Beta 11.2 introduces this module purely as the canonical *source*; it
deliberately preserves the exact contract that was previously inlined in
``content/templatetags/richtext.py`` verbatim, byte for byte. No tag,
attribute, protocol or CSS property is added, removed or altered here - the
public rendered output must stay identical (see
``core/tests/test_richtext_contract.py``).

Two behaviours of the current contract are load-bearing and intentionally
kept as-is in this slice (both flagged for a later, separate hardening
slice, never changed here):

* ``"data-*"`` / ``"aria-*"`` in :data:`ALLOWED_ATTRIBUTES` are literal
  attribute names to bleach, not globs - bleach does not expand wildcard
  patterns in a plain attribute list, so ``data-foo`` / ``aria-label`` are
  in fact *stripped* today. Preserving the exact list preserves that exact
  (stripping) behaviour.
* ``"data"`` is an allowed URL protocol, so ``<img src="data:...">`` is
  currently kept. This is preserved unchanged as the documented baseline,
  not endorsed as a final product decision.

This module contains only pure data and a pre-built sanitizer. It must not
import Django templates, models, settings, the active language, request
objects, or any TinyMCE-specific syntax - those belong to later slices that
*consume* this contract.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from bleach.css_sanitizer import CSSSanitizer

#: Allowed HTML tags. Copied verbatim (same members, same order) from the
#: pre-Beta-11.2 ``content.templatetags.richtext.ALLOWED_TAGS``.
ALLOWED_TAGS = [
    "p",
    "br",
    "hr",
    "blockquote",
    "pre",
    "code",
    "span",
    "h2",
    "h3",
    "h4",
    "h5",
    "ul",
    "ol",
    "li",
    "strong",
    "em",
    "u",
    "s",
    "sub",
    "sup",
    "b",
    "i",
    "a",
    "img",
    "figure",
    "figcaption",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "div",
    "section",
    "article",
    "header",
    "footer",
    "nav",
]

#: Allowed attributes per tag (``"*"`` = every tag). Copied verbatim from
#: the pre-Beta-11.2 contract. NOTE: ``"data-*"`` / ``"aria-*"`` are literal
#: names to bleach, not globs - see the module docstring.
ALLOWED_ATTRIBUTES = {
    "*": ["class", "id", "style", "data-*", "aria-*"],
    "a": ["href", "title", "rel", "target"],
    "img": ["src", "alt", "title", "width", "height", "loading"],
    "table": ["border", "cellpadding", "cellspacing"],
    "th": ["colspan", "rowspan"],
    "td": ["colspan", "rowspan"],
}

#: Allowed URL protocols. Copied verbatim. ``"data"`` is kept as the
#: documented baseline (see module docstring), not endorsed as final.
ALLOWED_PROTOCOLS = ["http", "https", "mailto", "data"]

#: Allowed inline-CSS properties. Copied verbatim (same members, same
#: order) from the ``CSSSanitizer`` argument that was previously inlined in
#: the filter module.
ALLOWED_CSS_PROPERTIES = [
    # Textdarstellung
    "color",
    "background-color",
    "text-align",
    "font-weight",
    "font-style",
    "text-decoration",
    "line-height",
    "letter-spacing",

    # Abstände
    "margin",
    "margin-top",
    "margin-right",
    "margin-bottom",
    "margin-left",
    "padding",
    "padding-top",
    "padding-right",
    "padding-bottom",
    "padding-left",

    # Größen
    "width",
    "height",
    "max-width",

    # Rahmen
    "border",
    "border-width",
    "border-style",
    "border-color",
    "border-radius",

    # Bilder
    "object-fit",

    # Tabellen
    "border-collapse",
    "border-spacing",
    "vertical-align",

    # Sonstiges (sicher)
    "display",  # inline, block – unkritisch
    "opacity",  # unkritisch
]

#: Pre-built, reusable CSS sanitizer bound to :data:`ALLOWED_CSS_PROPERTIES`.
#: Built once at import time exactly as the previous module did; the
#: renderer passes this instance straight through to ``bleach.clean``.
CSS_SANITIZER = CSSSanitizer(allowed_css_properties=ALLOWED_CSS_PROPERTIES)


@dataclass(frozen=True)
class RichtextContract:
    """
    Immutable, machine-readable view of the canonical contract.

    Bundles the four allow-lists as immutable structures so a later slice
    (e.g. Beta 11.3's TinyMCE ``valid_elements`` adapter) can read one
    authoritative source instead of re-deriving it. It is derived from the
    module-level lists above, which remain the values the renderer actually
    hands to bleach - so there is a single source of truth, not a parallel
    one. This class deliberately carries no TinyMCE syntax and no behaviour.
    """

    tags: tuple[str, ...]
    attributes: Mapping[str, tuple[str, ...]]
    protocols: tuple[str, ...]
    css_properties: tuple[str, ...]


#: The canonical contract as an immutable value object.
RICHTEXT_CONTRACT = RichtextContract(
    tags=tuple(ALLOWED_TAGS),
    attributes=MappingProxyType(
        {tag: tuple(attrs) for tag, attrs in ALLOWED_ATTRIBUTES.items()}
    ),
    protocols=tuple(ALLOWED_PROTOCOLS),
    css_properties=tuple(ALLOWED_CSS_PROPERTIES),
)
