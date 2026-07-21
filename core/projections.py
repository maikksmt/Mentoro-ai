"""
Public content projection: the values a visitor actually sees on a public
detail page, for one explicitly requested language.

Editorial content (guides, prompts, use cases, comparisons) keeps the last
published revision in the ``live_i18n`` JSON snapshot. That snapshot - not the
current translation row - is what the public surfaces show, because an editor
changing a title mid-review updates the translation row immediately while the
site must keep serving the published text.

The same rule is expressed twice here: once in Python for teasers and result
projection, once as a database expression for full-text search. They must stay
in agreement, which core/tests/test_projections.py asserts directly.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db.models import Case, Model, OuterRef, Q, Subquery, TextField, Value, When
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Coalesce
from django.urls import NoReverseMatch, reverse
from django.utils.translation import override

#: JSON field holding the published revision, keyed by language code.
PUBLIC_SNAPSHOT_FIELD = "live_i18n"

#: Returned when an object has no public revision in the requested language.
#: Matches the models' own convention for an unresolvable detail URL.
NO_PUBLIC_URL = "#"


def public_content_value(obj: Model, field: str, *, language_code: str) -> str:
    """
    Returns the publicly visible value of a translated field in
    `language_code`, never a draft value and never another language's.

    Three states, and the distinction between them is the whole point:

    A. ``live_i18n`` has an entry for `language_code` - that snapshot is the
       sole authority. An empty or absent field in it means the published
       revision genuinely has no text there, so the answer is "" and the
       current translation is never consulted.
    B. ``live_i18n`` is non-empty but has no entry for `language_code` - the
       object was published in other languages only. There is no public
       revision in this language, so the answer is "". A translation added
       after that publish is a draft and must stay invisible.
    C. ``live_i18n`` is entirely empty - a record predating the snapshot
       mechanism, or a fixture created without the publish transition. Only
       here may the current same-language translation stand in.

    Truthiness is deliberately never used to decide between the states: ""
    is a valid published value, not a missing one. Reading
    ``live_i18n[language_code]`` directly rather than the models'
    ``get_live_value()`` also avoids that helper's cross-language fallback,
    which is right for its own callers and wrong for a language-explicit
    public value.
    """
    if not language_code:
        raise ValueError("language_code is required")

    snapshot = getattr(obj, PUBLIC_SNAPSHOT_FIELD, None) or {}

    if snapshot:
        published = snapshot.get(language_code)
        if not isinstance(published, dict):
            return ""  # state B
        return published.get(field) or ""  # state A

    # state C
    has_translation = getattr(obj, "has_translation", None)
    if callable(has_translation) and not has_translation(language_code):
        return ""
    getter = getattr(obj, "safe_translation_getter", None)
    if callable(getter):
        return getter(field, language_code=language_code) or ""
    return getattr(obj, field, "") or ""


def public_content_value_expression(
    translation_model: type[Model],
    field: str,
    *,
    language_code: str,
):
    """
    The database-expression twin of :func:`public_content_value`, resolving
    the same three states A/B/C.

    The branch is chosen by JSONB key existence, never by whether the stored
    value happens to be empty: ``NULLIF(snapshot, '')`` would treat a
    published empty field as missing and fall through to the draft. The
    translation subquery therefore contributes only in state C.
    """
    if not language_code:
        raise ValueError("language_code is required")

    published_value = Coalesce(
        KeyTextTransform(
            field, KeyTextTransform(language_code, PUBLIC_SNAPSHOT_FIELD)
        ),
        Value(""),
        output_field=TextField(),
    )
    current_translation = Coalesce(
        Subquery(
            translation_model.objects.filter(
                master_id=OuterRef("pk"), language_code=language_code
            ).values(field)[:1]
        ),
        Value(""),
        output_field=TextField(),
    )
    return Case(
        # A: a snapshot exists for this language and is authoritative.
        When(
            **{f"{PUBLIC_SNAPSHOT_FIELD}__has_key": language_code},
            then=published_value,
        ),
        # C: no snapshot at all - legacy record, same-language translation.
        When(
            Q(**{PUBLIC_SNAPSHOT_FIELD: {}})
            | Q(**{f"{PUBLIC_SNAPSHOT_FIELD}__isnull": True}),
            then=current_translation,
        ),
        # B: published in other languages only - no public revision here.
        default=Value(""),
        output_field=TextField(),
    )


def public_content_url(obj: Model, *, language_code: str) -> str:
    """
    Returns the public detail URL for `language_code`, or ``"#"`` when the
    object has no public revision in that language.

    Built from the authoritative slug rather than delegated to the model's
    ``get_absolute_url()``: that method resolves its slug through
    ``get_live_value()``, which falls back across languages, so an object
    published in English only would hand out the English snapshot slug - or,
    once a German draft translation exists, that draft's slug - under the
    German prefix. Both are wrong here.

    ``reverse()`` runs inside ``override(language_code)`` so the
    i18n_patterns prefix matches the requested language rather than whatever
    is ambiently active.
    """
    if not language_code:
        raise ValueError("language_code is required")

    slug = public_content_value(
        obj, "public_slug", language_code=language_code
    ) or public_content_value(obj, "slug", language_code=language_code)
    if not slug:
        return NO_PUBLIC_URL

    with override(language_code):
        try:
            return reverse(
                f"{obj._meta.app_label}:detail", kwargs={"slug": slug}
            )
        except NoReverseMatch:
            return NO_PUBLIC_URL


@dataclass(frozen=True, slots=True)
class PublicContentProjection:
    """The public face of one content object in one language."""

    kind: str
    object_id: int
    language_code: str
    title: str
    summary: str
    body: str
    url: str
    published_at: datetime | None
    updated_at: datetime | None


def project_public_content(
    obj: Any,
    kind: str,
    *,
    language_code: str,
    summary_field: str = "intro",
) -> PublicContentProjection:
    """
    Projects a content object onto its public values in `language_code`.

    ``summary_field`` names the model's short-text field; every editorial model
    calls it ``intro``.
    """
    return PublicContentProjection(
        kind=kind,
        object_id=obj.pk,
        language_code=language_code,
        title=public_content_value(obj, "title", language_code=language_code),
        summary=public_content_value(obj, summary_field, language_code=language_code),
        body=public_content_value(obj, "body", language_code=language_code),
        url=public_content_url(obj, language_code=language_code),
        published_at=getattr(obj, "published_at", None),
        updated_at=getattr(obj, "updated_at", None),
    )
