"""
Beta 11.6: the handful of preview primitives that were byte-identical
between ``guides/presentation.py``+``guides/admin.py`` and
``prompts/presentation.py``+``prompts/admin.py`` after Beta 11.4/11.5
(``prompts/presentation.py`` even said as much - its ``has_saved_translation``
docstring called the duplication "deferred to Beta 11.6").

Only what was technically identical moves here: a translation-existence
check, the preview language guard, the three response headers, the shared
robots directive, and the SEO object shape. Everything else - the view
methods, URL wiring, template selection, related-content lookups,
``translation.override()`` usage, permission checks - stays in
``guides/admin.py`` and ``prompts/admin.py`` exactly as it was, because it
differs per content type (or would only coincidentally look alike).

This module intentionally knows nothing about Guide, Prompt, ModelAdmin,
HttpRequest or templates, so importing it here can never create a cycle with
``content``/``guides``/``prompts``.
"""
from __future__ import annotations

from django.conf import settings
from django.utils.cache import patch_cache_control

from core.seo.types import SeoMeta

#: Robots directive sent for every preview response (header and meta alike).
PREVIEW_ROBOTS = "noindex,nofollow"


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


def is_supported_preview_language(language_code: str) -> bool:
    """True when ``language_code`` is one of the project's ``settings.LANGUAGES``."""
    return language_code in {code for code, _label in settings.LANGUAGES}


def apply_editorial_preview_headers(response, language_code: str):
    """
    Stamp the non-cacheable, non-indexable contract onto a preview response.

    ``Cache-Control`` is guaranteed by this helper itself via Django's own
    ``patch_cache_control()`` - the same primitive ``never_cache`` is built
    on - rather than being left to ``admin_site.admin_view()``'s
    ``never_cache`` wrapper alone. ``patch_cache_control()`` merges with
    whatever ``Cache-Control`` is already on the response (parsing existing
    directives instead of overwriting the header), so this is safe to call
    either before or after ``never_cache`` runs, and safe to call more than
    once: every directive here is idempotent, and an existing ``max-age``
    can only ever be lowered towards this helper's ``0``, never raised.
    """
    patch_cache_control(
        response,
        private=True,
        no_store=True,
        no_cache=True,
        must_revalidate=True,
        max_age=0,
    )
    response["X-Robots-Tag"] = PREVIEW_ROBOTS
    response["Pragma"] = "no-cache"
    response["Content-Language"] = language_code
    return response


def build_preview_seo_meta(*, title: str, description: str) -> SeoMeta:
    """
    The minimal SEO object every preview renders with.

    Deliberately empty beyond title/description: no canonical, no
    alternates, no JSON-LD, and ``robots`` fixed to :data:`PREVIEW_ROBOTS`.
    That keeps the preview URL out of canonical links, hreflang and
    structured data, and avoids advertising a public URL for content that
    may never have been published.
    """
    return SeoMeta(
        title=title,
        description=description,
        date="",
        author="",
        og_type="article",
        canonical="",
        robots=PREVIEW_ROBOTS,
        og_image=None,
        alternates=[],
        json_ld=None,
    )
