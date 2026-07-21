"""Template access to the search kind presentations."""
from django import template

from search.presentation import presentation_for

register = template.Library()


@register.simple_tag
def search_kind_label(kind):
    """Singular term for a result badge."""
    return presentation_for(kind).label


@register.simple_tag
def search_kind_plural_label(kind):
    """Plural term for the per-type counts."""
    return presentation_for(kind).plural_label


@register.simple_tag
def search_kind_badge_class(kind):
    """Existing project badge class for a content kind."""
    return presentation_for(kind).badge_class
