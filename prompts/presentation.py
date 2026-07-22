"""
Beta 11.5: the saved-draft context for the admin prompt preview.

Mirrors the Beta 11.4 guide-preview architecture (see
``guides/presentation.py`` for the full rationale), scoped to what Prompt
actually needs. Unlike Guide, ``templates/prompts/prompt_detail.html``
already renders exclusively through context variables (``display_title``,
``display_intro``, ``display_body``, ``display_outro``, ``object.tools.all``,
``object.author``, ...) - it never calls a snapshot-first model method
directly. So no template change and no per-field view dataclass are needed
here: the public view already had an implicit "public context" contract;
this module adds the explicit "saved draft" counterpart to the same
contract, and the two never mix.

Public and draft stay two independent functions with independent sources:

* The public path is unchanged - ``PromptDetailView.get_context_data()``
  still reads ``obj.display_title``/``display_intro``/``display_body``/
  ``display_outro`` (snapshot-first, ``live_i18n``) exactly as before.
* :func:`build_draft_prompt_context` reads the stored translation row for
  one explicitly requested language and never consults ``live_i18n``,
  ``get_display_value()`` or any cross-language fallback.

:func:`has_saved_translation` moved to ``core.editorial_preview`` in Beta
11.6, once this module's byte-identical copy of
``guides.presentation.has_saved_translation`` gave the duplication a second
occurrence worth consolidating (see the Beta 11.5 final report's
"duplicate vs. extract" reasoning for why one occurrence alone was not).
"""
from __future__ import annotations

from django.urls import reverse
from django.utils.translation import gettext as _

from core.editorial_preview import build_preview_seo_meta
from core.seo.utils import seo_text
from core.services import related_prompts, to_teaser_item


def _draft_translation_value(obj, field: str, language_code: str) -> str:
    """Stored translation value for exactly ``language_code``, or ``""``."""
    return (
        obj.safe_translation_getter(
            field, language_code=language_code, any_language=False
        )
        or ""
    )


def build_draft_prompt_context(prompt, language_code: str) -> dict:
    """
    Full context for rendering a saved prompt draft through the real public
    detail template.

    Must be called inside ``translation.override(language_code)`` so that
    ``{% trans %}``, ``reverse()`` and the related-content lookups all resolve
    in the previewed language rather than the editor's ambient one.

    ``object.tools.all``, ``object.author``, ``object.published_at`` and
    ``object.updated_at`` are read straight off ``prompt`` by the template -
    none of them are translated or snapshotted, so the preview shows them
    exactly as the public page already would (see the Beta 11.5 final
    report's draft-data-source section for why this needs no new handling).

    Related content deliberately reuses the ordinary public helper: it runs
    against ``visible_in_language()`` and the live-revision-safe teaser
    values, so the preview's "More Prompts" block shows public data only and
    cannot surface anyone else's draft.

    The SEO object is intentionally minimal: ``noindex,nofollow`` plus an
    empty canonical, no alternates and no JSON-LD. That keeps the preview URL
    out of canonical links, hreflang and structured data, and avoids
    advertising a public URL for a prompt that may never have been published.
    """
    title = _draft_translation_value(prompt, "title", language_code)
    intro = _draft_translation_value(prompt, "intro", language_code)
    body = _draft_translation_value(prompt, "body", language_code)
    outro = _draft_translation_value(prompt, "outro", language_code)

    related = related_prompts(prompt, limit=3, language_code=language_code)

    return {
        "object": prompt,
        "display_title": title,
        "display_intro": intro,
        "display_body": body,
        "display_outro": outro,
        "more": [
            to_teaser_item(p, "prompt", language_code=language_code) for p in related
        ],
        "crumbs": [
            (_("Prompts"), reverse("prompts:list")),
            (title, ""),
        ],
        "seo": build_preview_seo_meta(
            title=title,
            description=seo_text(intro or body or title)[:155],
        ),
        "is_preview": True,
        "preview_language": language_code,
    }
