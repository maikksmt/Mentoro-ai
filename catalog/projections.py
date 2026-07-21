"""
Strictly language-bound public projection of a tool.

Tools have no editorial workflow and no ``live_i18n`` snapshot, so there is no
published revision to resolve against - the translation row for the requested
language *is* the public value. What this module adds is the guarantee that
the row actually belongs to that language, so no parler fallback and no
ambient language can slip in.

This is deliberately not the semantics of the tool catalogue. There, an
English-only tool intentionally appears on the German pages with its English
text (see ToolQuerySet's docstring and catalog/tests/test_language_fallback.py).
That behaviour is unchanged; only the global search is strict.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.db.models import Model


@dataclass(frozen=True, slots=True)
class PublicToolProjection:
    """The public face of one tool in one language."""

    object_id: int
    language_code: str
    title: str
    summary: str
    body: str
    url: str
    vendor: str
    published_at: datetime | None
    updated_at: datetime | None


def public_tool_url(tool: Model, *, language_code: str) -> str:
    """
    Returns the internal detail URL for `language_code`.

    The slug lives on the tool itself rather than on a translation, so it is
    shared across languages; only the i18n_patterns prefix differs. Delegates
    to the model's own ``get_absolute_url(language=...)``, which already
    reverses inside ``override()`` - and never returns the external
    ``Tool.website``.
    """
    if not language_code:
        raise ValueError("language_code is required")
    return tool.get_absolute_url(language=language_code)


def project_public_tool(translation: Model, *, language_code: str) -> PublicToolProjection:
    """
    Projects a :class:`~catalog.models.ToolTranslation` row onto its public
    values.

    Takes the translation row rather than the tool so the language binding is
    structural: the caller must already hold the row for `language_code`,
    which the query established. The mismatch check below turns a wrong row
    into an error instead of a silently mistranslated result.
    """
    if not language_code:
        raise ValueError("language_code is required")
    if translation.language_code != language_code:
        raise ValueError(
            f"translation is in {translation.language_code!r}, "
            f"expected {language_code!r}"
        )

    tool = translation.master
    return PublicToolProjection(
        object_id=tool.pk,
        language_code=language_code,
        title=translation.name or "",
        summary=translation.short_description or "",
        body=translation.long_description or "",
        url=public_tool_url(tool, language_code=language_code),
        vendor=tool.vendor or "",
        published_at=tool.published_at,
        updated_at=tool.updated_at,
    )
