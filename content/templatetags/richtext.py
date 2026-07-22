"""
The ``richtext`` template filter.

Since Beta 11.2 this module is a thin adapter: the canonical rich-text
contract and sanitization live in :mod:`core.richtext`, and the filter (plus
the Beta 11.1 readonly-admin display methods that import ``richtext`` from
here) delegate to :func:`core.richtext.render_content`. The filter name,
call signature, importability and ``SafeString`` return type are unchanged.
"""
from django import template

from core.richtext import render_content

register = template.Library()


@register.filter(name="richtext")
def richtext(value):
    """Sanitize ``value`` via the canonical renderer and return a ``SafeString``."""
    return render_content(value)
