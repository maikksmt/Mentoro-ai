"""
Beta 11.4: the explicit rendering contract for the public guide detail
template, plus the saved-draft context the admin preview renders through it.

Why this module exists
----------------------
``templates/guides/guide_detail.html`` used to resolve its section and item
values by calling model methods straight from the template
(``section.display_title``, ``item.get_title()``, ...). Those methods are
snapshot-first (``GuideSection.get_display_value()`` prefers ``live_i18n``)
and Parler-fallback-prone (``GuideItem.title`` is a translated-field
descriptor with ``use_fallback=True``). That is exactly right for the public
page, but it makes a *saved-draft* preview impossible without either a
global "preview mode" inside the model methods or a hidden data-source
switch on the active language - both explicitly ruled out.

So the two data sources are separated here instead, as two plain functions
over one shared template contract:

* :func:`public_display_sections` - the published/live source. It calls the
  very same model methods the template called inline before, over the very
  same prefetched querysets, so the rendered public DOM is unchanged.
* :func:`draft_display_sections` / :func:`build_draft_guide_context` - the
  saved-draft source. It reads the stored translation rows for one
  explicitly requested language and never consults ``live_i18n``,
  ``get_display_value()`` or any cross-language fallback.

The public view never sees a preview flag, and the preview never runs
through the public visibility queryset.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.urls import reverse
from django.utils.translation import gettext as _

from core.seo.types import SeoMeta
from core.seo.utils import seo_text
from core.services import related_guides, to_teaser_item

#: Robots directive sent for every preview response (header and meta alike).
PREVIEW_ROBOTS = "noindex,nofollow"


@dataclass(frozen=True)
class GuideItemView:
    """One already-resolved guide item, as the detail template consumes it."""

    url: str
    title: str
    kind: str
    teaser: str


@dataclass(frozen=True)
class GuideSectionView:
    """One already-resolved guide section, as the detail template consumes it."""

    title: str
    body: str
    items: tuple[GuideItemView, ...]


def public_display_sections(guide) -> list[GuideSectionView]:
    """
    Published/live sections for the public detail page.

    Deliberately a 1:1 lift of what ``guide_detail.html`` evaluated inline
    before Beta 11.4: the same ``guide.sections.all()`` /
    ``section.items.all()`` prefetch caches (so no extra queries and the same
    ordering and language filtering), the same snapshot-first
    ``display_title``/``display_body``, the same ``is_published`` item gate,
    and the same ``get_url``/``get_title``/``get_teaser`` accessors. Values
    are passed through untouched - including ``None`` - so the rendered
    output stays byte-identical.

    Must be called with the request's language active, exactly as the
    template rendering is; the model methods below read ``get_language()``.
    """
    sections: list[GuideSectionView] = []
    for section in guide.sections.all():
        items = tuple(
            GuideItemView(
                url=item.get_url(),
                title=item.get_title(),
                kind=item.kind,
                teaser=item.get_teaser(),
            )
            for item in section.items.all()
            if item.is_published
        )
        sections.append(
            GuideSectionView(
                title=section.display_title,
                body=section.display_body,
                items=items,
            )
        )
    return sections


def draft_display_sections(guide, language_code: str) -> list[GuideSectionView]:
    """
    Saved-draft sections for the preview, in one explicit language.

    Reads the stored translation rows directly - never ``live_i18n``, never
    ``get_display_value()``, never ``any_language=True``. Ordering is the
    currently saved ``order`` (not a snapshot), and items keep the public
    ``is_published`` gate so the preview cannot show something the public
    page would hide.

    Fail-closed on language: a section or item without a stored translation
    in ``language_code`` is omitted rather than rendered from another
    language. ``set_current_language()`` is called only after
    :func:`has_saved_translation` confirmed the row exists, so the Parler
    descriptors used by ``get_title()``/``get_teaser()`` resolve that row and
    can never fall back to the ``PARLER_LANGUAGES`` fallback language.
    """
    sections: list[GuideSectionView] = []
    for section in guide.sections.order_by("order", "id"):
        if not has_saved_translation(section, language_code):
            continue
        section.set_current_language(language_code)

        items: list[GuideItemView] = []
        for item in section.items.filter(is_published=True).order_by("order", "id"):
            if not has_saved_translation(item, language_code):
                continue
            item.set_current_language(language_code)
            items.append(
                GuideItemView(
                    url=item.get_url(),
                    title=item.get_title(),
                    kind=item.kind,
                    teaser=item.get_teaser(),
                )
            )

        sections.append(
            GuideSectionView(
                title=_draft_translation_value(section, "title", language_code),
                body=_draft_translation_value(section, "body", language_code),
                items=tuple(items),
            )
        )
    return sections


def has_saved_translation(obj, language_code: str) -> bool:
    """
    True when a translation row for ``language_code`` is actually stored.

    Deliberately queried instead of using Parler's ``has_translation()``:
    ``TranslatableAdmin.get_object()`` calls
    ``set_current_language(..., initialize=True)`` for the tab the editor has
    open, which puts an *empty, unsaved* translation into the instance cache.
    ``has_translation()`` then reports True for a language the author never
    saved - which would offer a preview link (and let the preview itself
    proceed) for content that does not exist. Counting rows is immune to that.
    """
    return obj.translations.filter(language_code=language_code).exists()


def _draft_translation_value(obj, field: str, language_code: str) -> str:
    """Stored translation value for exactly ``language_code``, or ``""``."""
    return (
        obj.safe_translation_getter(
            field, language_code=language_code, any_language=False
        )
        or ""
    )


def build_draft_guide_context(guide, language_code: str) -> dict:
    """
    Full context for rendering a saved guide draft through the real public
    detail template.

    Must be called inside ``translation.override(language_code)`` so that
    ``{% trans %}``, ``reverse()`` and the related-content lookups all resolve
    in the previewed language rather than the editor's ambient one.

    Related content deliberately reuses the ordinary public helpers: they run
    against ``visible_in_language()`` and the live-revision-safe teaser
    values, so the preview's "Related Guides" block shows public data only
    and cannot surface anyone else's draft.

    The SEO object is intentionally minimal: ``noindex,nofollow`` plus an
    empty canonical, no alternates and no JSON-LD. That keeps the preview URL
    out of canonical links, hreflang and structured data, and avoids
    advertising a public URL for a guide that may never have been published.
    """
    title = _draft_translation_value(guide, "title", language_code)
    intro = _draft_translation_value(guide, "intro", language_code)
    body = _draft_translation_value(guide, "body", language_code)

    related = related_guides(guide, limit=3, language_code=language_code)

    return {
        "object": guide,
        "display_title": title,
        "display_intro": intro,
        "display_body": body,
        "display_sections": draft_display_sections(guide, language_code),
        "related_guides": [
            to_teaser_item(g, "guide", language_code=language_code) for g in related
        ],
        "crumbs": [
            (_("Guides"), reverse("guides:list")),
            (title, ""),
        ],
        "seo": SeoMeta(
            title=title,
            description=seo_text(intro or body or title)[:155],
            date="",
            author="",
            og_type="article",
            canonical="",
            robots=PREVIEW_ROBOTS,
            og_image=None,
            alternates=[],
            json_ld=None,
        ),
        "is_preview": True,
        "preview_language": language_code,
    }
