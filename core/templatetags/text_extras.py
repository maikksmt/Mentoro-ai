from django import template

from core.text import EDITORIAL_INTRO_MAX_CHARS, summarize_html

register = template.Library()


@register.filter(name="summarize")
def summarize(value, max_chars: int = EDITORIAL_INTRO_MAX_CHARS) -> str:
    """
    Renders a rich-text field as a short plain-text teaser.

    Returns a plain ``str`` on purpose, so the template layer autoescapes the
    result like any other variable.
    """
    return summarize_html(value, int(max_chars))
