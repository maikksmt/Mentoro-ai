"""
How a content kind is shown on the search page.

Presentation only: labels and badge classes. The domain enum stays free of
display strings, and nothing here touches ranking, visibility or models.
Labels reuse the terms the rest of the site already uses for these types.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _

from search.result_types import SearchResultKind


@dataclass(frozen=True, slots=True)
class KindPresentation:
    """The label and badge style for one content kind."""

    #: Singular term, used on a result's badge.
    label: str
    #: Plural term, used in the per-type counts.
    plural_label: str
    #: Existing project badge class - no new CSS taxonomy.
    badge_class: str


KIND_PRESENTATIONS: dict[SearchResultKind, KindPresentation] = {
    SearchResultKind.TOOL: KindPresentation(_("Tool"), _("Tools"), "badge-tool"),
    SearchResultKind.GUIDE: KindPresentation(_("Guide"), _("Guides"), "badge-guide"),
    SearchResultKind.PROMPT: KindPresentation(_("Prompt"), _("Prompts"), "badge-prompt"),
    SearchResultKind.USE_CASE: KindPresentation(
        _("Use case"), _("Use cases"), "badge-usecase"
    ),
    SearchResultKind.COMPARISON: KindPresentation(
        _("Comparison"), _("Comparisons"), "badge-compare"
    ),
}


def presentation_for(kind: SearchResultKind) -> KindPresentation:
    return KIND_PRESENTATIONS[kind]
