"""
Canonical rich-text contract and renderer (Beta 11.2).

Public API:

* :func:`~core.richtext.render.render_content` - the one server-side
  renderer for author-authored rich text.
* :data:`~core.richtext.contract.RICHTEXT_CONTRACT` /
  :class:`~core.richtext.contract.RichtextContract` - the immutable,
  machine-readable contract a later TinyMCE adapter can consume.
"""
from core.richtext.contract import RICHTEXT_CONTRACT, RichtextContract
from core.richtext.render import render_content

__all__ = ["render_content", "RICHTEXT_CONTRACT", "RichtextContract"]
