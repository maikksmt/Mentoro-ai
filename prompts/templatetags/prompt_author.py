"""
Beta 11.11C4F: thin template adapter for
``prompts.live_author.resolve_prompt_live_author_display_name`` - exists only
because ``{% include %}`` cannot call a Python function with an argument
directly. Contains no validation, query, or fallback logic of its own; see
``prompts/live_author.py`` for the actual contract.
"""
from django import template

from prompts.live_author import resolve_prompt_live_author_display_name

register = template.Library()


@register.filter(name="resolve_prompt_author")
def resolve_prompt_author(prompt) -> str:
    return resolve_prompt_live_author_display_name(prompt)
