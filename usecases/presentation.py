"""
Beta 11.8: the saved-draft context for the admin use-case preview.

Mirrors the Beta 11.4/11.5 guide/prompt-preview architecture (see
``prompts/presentation.py`` for the full rationale - UseCase has no
sections/items, exactly like Prompt). Unlike Guide,
``templates/usecases/detail.html`` already renders exclusively through
context variables (``display_title``, ``display_intro``, ``display_body``,
``display_outro``, ``object.tools.all``, ``object.author``,
``object.published_at``, ``object.updated_at``, ...) - it never calls a
snapshot-first model method directly. So no template change and no per-field
view dataclass are needed here: the public view already had an implicit
"public context" contract; this module adds the explicit "saved draft"
counterpart to the same contract, and the two never mix.

Public and draft stay two independent functions with independent sources:

* The public path is unchanged - ``UseCaseDetailView.get_context_data()``
  still reads ``obj.display_title``/``display_intro``/``display_body``/
  ``display_outro``/``display_persona`` (snapshot-first, ``live_i18n``)
  exactly as before.
* :func:`build_draft_usecase_context` reads the stored translation row for
  one explicitly requested language and never consults ``live_i18n``,
  ``get_display_value()`` or any cross-language fallback.

``display_persona`` is included in the returned context for the same reason
the public view includes it: ``UseCaseDetailView.get_context_data()`` sets it
unconditionally, even though ``templates/usecases/detail.html`` does not
currently render it anywhere. This module mirrors that context shape - the
draft persona goes into the same key, from the same kind of source
(translation row instead of snapshot) - without adding any new visible
persona element. If the template ever starts rendering it, the preview
already carries the right (draft) value; until then it is inert, exactly as
inert as it already is on the public page.
"""
from __future__ import annotations

from django.urls import reverse
from django.utils.translation import gettext as _

from core.editorial_preview import build_preview_seo_meta
from core.seo.utils import seo_text
from core.services import related_usecases, to_teaser_item


def _draft_translation_value(obj, field: str, language_code: str) -> str:
    """Stored translation value for exactly ``language_code``, or ``""``."""
    return (
        obj.safe_translation_getter(
            field, language_code=language_code, any_language=False
        )
        or ""
    )


def build_draft_usecase_context(usecase, language_code: str) -> dict:
    """
    Full context for rendering a saved use-case draft through the real
    public detail template.

    Must be called inside ``translation.override(language_code)`` so that
    ``{% trans %}``, ``reverse()`` and the related-content lookups all resolve
    in the previewed language rather than the editor's ambient one.

    ``object.author``, ``object.published_at`` and ``object.updated_at`` are
    read straight off ``usecase`` by the template - none of them are
    translated or snapshotted, so the preview shows them exactly as the public
    page already would. Since Beta 11.11D4B the linked tools are the one
    exception: they are passed explicitly, because the public view now filters
    them through ``Tool.objects.public()`` while the preview keeps showing the
    saved draft M2M.

    Related content deliberately reuses the ordinary public helper: it runs
    against ``visible_in_language()`` and the live-revision-safe teaser
    values (``core.projections.public_content_value``/``public_content_url``
    under the hood), so the preview's "Similar use cases" block shows public
    data only and cannot surface anyone else's draft. ``related_usecases()``
    itself reads the *source* use case's persona from its own live snapshot
    (Beta 11.7B) - this function passes the draft ``usecase`` instance
    through unchanged, so a draft persona edit here has exactly as much
    effect on the ranking as it already has on the public page: none, until
    republished.

    The SEO object is intentionally minimal: ``noindex,nofollow`` plus an
    empty canonical, no alternates and no JSON-LD. That keeps the preview URL
    out of canonical links, hreflang and structured data, and avoids
    advertising a public URL for a use case that may never have been
    published.
    """
    title = _draft_translation_value(usecase, "title", language_code)
    intro = _draft_translation_value(usecase, "intro", language_code)
    body = _draft_translation_value(usecase, "body", language_code)
    outro = _draft_translation_value(usecase, "outro", language_code)
    persona = _draft_translation_value(usecase, "persona", language_code)

    related = related_usecases(usecase, limit=3, language_code=language_code)

    return {
        "object": usecase,
        "display_title": title,
        "display_intro": intro,
        "display_body": body,
        "display_outro": outro,
        "display_persona": persona,
        # Beta 11.11D4B: the template now renders the `tools` context variable
        # instead of `object.tools.all`, so the preview has to supply it too.
        # It deliberately passes the *saved draft* M2M unfiltered: the preview
        # exists to show the editor the draft as saved, and D4B is scoped to
        # the public projection only (the public view passes
        # `obj.tools.public()`). Preview output is therefore byte-identical to
        # before this slice.
        "tools": list(usecase.tools.all()),
        "similar": [
            to_teaser_item(u, "usecase", language_code=language_code) for u in related
        ],
        "crumbs": [
            (_("Usecases"), reverse("usecases:list")),
            (title, ""),
        ],
        "seo": build_preview_seo_meta(
            title=title,
            description=seo_text(intro or body or title)[:155],
        ),
        "is_preview": True,
        "preview_language": language_code,
    }
