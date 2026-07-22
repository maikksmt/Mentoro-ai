"""
The single canonical server-side rich-text renderer.

``render_content(value)`` is the one function that turns stored,
author-authored rich text into HTML that is safe to place into the page. It
is a faithful extraction of the sanitization that previously lived inline in
``content.templatetags.richtext.richtext`` - same ``bleach.clean`` call,
same contract (imported from :mod:`core.richtext.contract`), same
``strip=True`` semantics - so its output is identical to the pre-Beta-11.2
filter for every input (see ``core/tests/test_richtext_contract.py``).

Guarantees:

* uses only the canonical contract; defines no allow-list of its own
* deterministic and idempotent: ``render_content(render_content(v)) ==
  render_content(v)``
* mutates no input, runs no database query, reads no active language and no
  settings, performs no URL rewriting and no post-sanitization DOM editing
* adds no classes, ids or attributes
* returns a Django :class:`~django.utils.safestring.SafeString`; only the
  already-sanitized result is ever marked safe - raw input is never passed
  through ``mark_safe``.
"""
from __future__ import annotations

import bleach
from django.utils.safestring import SafeString, mark_safe

from core.richtext.contract import (
    ALLOWED_ATTRIBUTES,
    ALLOWED_PROTOCOLS,
    ALLOWED_TAGS,
    CSS_SANITIZER,
)


def render_content(value) -> SafeString:
    """
    Sanitize ``value`` against the canonical rich-text contract and return
    it as a ``SafeString``.

    Falsy input (``None``, ``""``, and other falsy values, mirroring the
    previous filter's ``if not html`` short-circuit) yields an empty
    ``SafeString`` - inert, and observationally identical to the previous
    bare-``""`` return in every template and admin context. Non-empty input
    is handed to ``bleach.clean`` exactly as before; the marking as safe
    happens only on the sanitized result.
    """
    if not value:
        return mark_safe("")
    cleaned = bleach.clean(
        value,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        protocols=ALLOWED_PROTOCOLS,
        css_sanitizer=CSS_SANITIZER,
        strip=True,
    )
    return mark_safe(cleaned)
