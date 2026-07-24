"""
Beta 11.11C1: the canonical, read-only review payload for one Prompt.

:func:`build_prompt_review_payload` answers exactly one question: "what does
the currently *stored* draft of this prompt actually say?" - as a JSON-native
dict that a later slice will hand to
``core.review_binding.fingerprint_review_payload()``. This module does not
compute or store a fingerprint itself; it only builds the payload that would
be fingerprinted.

Everything here is read-only. No ``save()``, no ``update()``, no M2M
mutation, no reversion context, no cache write - see
``prompts/tests/test_review_payload.py`` for the mutation-freedom and
query-budget tests that hold this to that contract.

Source of truth
----------------
The ``prompt`` argument is used only to determine the concrete model, the
primary key, and the database alias. Every field in the payload is read from
a *fresh* database query issued inside this function - never from attributes
already sitting on the caller's possibly-stale Python instance, never through
Parler's ``safe_translation_getter()`` (which can silently fall back across
languages), and never through the caller's currently active language. A
caller holding an outdated in-memory ``Prompt``, or one with unsaved local
edits, gets exactly the same payload as a caller that just reloaded it from
the database.

What is in the payload, and why
--------------------------------
Prompt has no root content field of its own beyond the shared editorial
workflow fields (all excluded, see below) - so ``fields`` is empty today.
Everything reviewable content-wise lives in translations or in two relations:

* **translations** - every stored ``PromptTranslation`` row, all six public
  fields (``title``, ``intro``, ``body``, ``outro``, ``slug``,
  ``public_slug``), sorted by normalized language code. All are rendered on
  the public detail page or used to resolve its URL - see
  ``prompts/views.py::_resolve_by_slug()`` and
  ``templates/prompts/prompt_detail.html``.
* **relations.tools** - the linked ``Tool`` ids only, sorted ascending.
  Tools are rendered richly on the detail page (name, vendor, logo, rating -
  see the "Suitable tools" section of ``prompt_detail.html``), but ``Tool``
  has no editorial workflow of its own; the same distinction Beta 11.11B1's
  ``core.reversion_registration`` and ``compare.models.Comparison.
  build_live_entries()`` already draw for exactly this reason ("membership
  travels along, the referenced object's own display attributes do not") is
  applied here. Only *which* tools are linked is part of what a reviewer
  reviewed; a tool renaming itself is not this prompt's concern.
* **relations.tags** - every tag's ``slug`` and ``name``, sorted by
  ``(slug, name, id)``. Prompt tags are *not* currently rendered as visible
  text anywhere on the site (confirmed via
  ``templates/prompts/prompt_list.html``/``prompt_detail.html`` and
  ``search/adapters/prompts.py``'s explicit exclusion) - they only drive
  ``core.services.related_prompts()``'s similarity ranking. Membership is
  still included unconditionally, because a later slice's stale-detection
  must be able to notice a changed tag set even though C1 does not yet wire
  that check to anything (see the Beta 11.11B1 taggit-boundary note below).
* **relations.author** - ``{"id": ...}`` or ``None``. Beta 11.11C4D
  narrowed this to *only* the relational identity
  (``prompt.author_id``) - the redactional question this payload exists to
  answer is "who is this prompt's author", not "what does that person's
  account currently display as their name". A real author reassignment
  (``author_id`` changing, including to/from ``None``) still changes this
  payload and its fingerprint, exactly as before. A change to the author's
  ``auth.User`` account - ``username``, ``first_name``, ``last_name``, or
  anything derived from them (``get_full_name()``) - does **not**, and never
  did substantively belong here: those are display concerns for a future,
  separate publish-time snapshot (Beta 11.11C4C audit), not part of what a
  reviewer reviewed. Before C4D this field carried
  ``{"username": ..., "display_name": ...}``, computed from the live
  ``auth.User`` row at payload-build time - see
  ``prompts/migrations/0008_prompt_review_payload_v2.py`` for the frozen
  historical serializer that reproduces that exact prior shape, used only to
  migrate already-bound ``review``/``approved`` rows onto this new contract.

Explicitly excluded (never influence the payload or its fingerprint):
``status``, ``review_revision``/``review_revision_id``,
``approved_revision``/``approved_revision_id``, ``review_payload_fingerprint``,
``reviewed_by``/``reviewed_by_id``, ``reviewed_at``, ``submitted_for_review_at``,
``review_note``, ``created_at``, ``updated_at``, ``published_at``,
``is_published``, ``live_i18n``, ``last_published_revision_id``, any
reversion id, cache state, request data, the active/fallback language, admin
form state, preview headers, any absolute/host-qualified URL, and - as of
Beta 11.11C4D - the author's ``username``, ``first_name``, ``last_name``,
``get_full_name()``, email, or any other account/profile attribute beyond the
bare ``author_id``.

No category: Prompt has no category field at all (unlike Guide), so there is
nothing to include or exclude on that account - confirmed against the real
model rather than assumed.

Fields not on the model that a public prompt template happens to probe
(``object.reading_time``) are not in the payload either: Django templates
render nothing for a missing attribute, so that reference is dead template
code, not a real data point.

No transactional read snapshot
-------------------------------
This is an ordinary read-builder, not a locking primitive: no
``select_for_update()``, no ``transaction.atomic()``. A later submit action
(Beta 11.11C2) that needs the payload and the review-revision binding to be
consistent with each other is responsible for calling this inside its own
atomic, locked context - exactly the pattern
``core.review_binding.invalidate_editorial_review_state()`` (Beta 11.11B2B2)
already established for the write side.

Not in C1: nothing here is called by Submit, Approve, Publish, the admin, or
any signal. See ``prompts/tests/test_review_payload.py`` for the static
checks that confirm this module is not yet imported anywhere else.
"""
from enum import StrEnum
from typing import Any

from django.db import DEFAULT_DB_ALIAS, connections

from catalog.models import Tool
from prompts.models import Prompt, PromptTranslation

#: Stable, versioned top-level schema identifier. Never derived from package,
#: git or database state - bumped by hand whenever the payload shape changes
#: in a way that should be treated as a new contract. Beta 11.11C4D bumped
#: v1 -> v2 for exactly one reason: the author section narrowed from
#: ``{"username": ..., "display_name": ...}`` to ``{"id": ...}`` - see the
#: module docstring and ``prompts/migrations/0008_prompt_review_payload_v2.py``
#: for how already-bound v1 rows are migrated onto this contract.
SCHEMA = "prompt-review-v2"
CONTENT_TYPE = "prompt"

#: Explicit whitelist of ``PromptTranslation`` fields carried into the
#: payload, in a fixed serialization order. Every one of these is rendered on
#: the public detail page or used to resolve its URL; ``id`` and ``master``
#: are Parler-internal and excluded. A new model column never joins this list
#: implicitly - see the module docstring.
TRANSLATION_FIELDS: tuple[str, ...] = ("title", "intro", "body", "outro", "slug", "public_slug")


class PromptReviewPayloadErrorCode(StrEnum):
    """Stable reason :func:`build_prompt_review_payload` refused to run,
    independent of message wording."""

    UNSUPPORTED_OBJECT = "unsupported_object"
    UNSAVED_OBJECT = "unsaved_object"
    INVALID_DATABASE_ALIAS = "invalid_database_alias"
    DATABASE_ALIAS_MISMATCH = "database_alias_mismatch"
    OBJECT_NOT_FOUND = "object_not_found"


class PromptReviewPayloadError(ValueError):
    """Raised by :func:`build_prompt_review_payload` for every input or alias
    problem it refuses to proceed past. Carries a stable :attr:`code`,
    mirroring ``core.review_binding``'s error classes."""

    def __init__(self, code: PromptReviewPayloadErrorCode, message: str):
        self.code = code
        super().__init__(message)


def _normalize_language_code(language_code: str) -> str:
    """Sort key only - the payload itself always carries the stored
    ``language_code`` verbatim, unnormalized."""
    return language_code.strip().lower()


def _serialize_translation(row: dict[str, Any]) -> dict[str, Any]:
    return {"language_code": row["language_code"], **{name: row[name] for name in TRANSLATION_FIELDS}}


def _serialize_author(author_id: int | None) -> dict[str, int] | None:
    """
    Beta 11.11C4D: the redactional author identity is exactly
    ``prompt.author_id`` - never the account's current display name (see the
    module docstring). Takes the raw FK id, not a ``User`` instance, so this
    never needs its own query against ``auth_user``.
    """
    if author_id is None:
        return None
    return {"id": author_id}


def build_prompt_review_payload(prompt: Any, *, using: str | None = None) -> dict[str, Any]:
    """
    Canonical, JSON-native review payload for the currently stored draft of
    ``prompt``.

    Accepts only a saved ``prompts.Prompt`` instance (or a proxy of it,
    resolved via ``prompt._meta.concrete_model``) - never a translation, a
    child model, another editorial type, a queryset, a list, or ``None``.
    Raises :class:`PromptReviewPayloadError` with a stable
    :class:`PromptReviewPayloadErrorCode` for every such case, and for an
    unsaved object, an unknown/mismatched database alias, or a database row
    that no longer exists. A malformed ``using`` argument raises
    ``TypeError``.

    Checked in this fixed order, so an alias that simply does not exist is
    never misreported as merely conflicting with the object's own alias:

    1. object type,
    2. primary key,
    3. ``using`` type,
    4. ``using`` alias existence (if given),
    5. ``using`` vs. the object's own already-set alias (if both present),
    6. the actual database row.

    Database alias resolution: explicit ``using``, then ``prompt._state.db``
    if set, then ``DEFAULT_DB_ALIAS`` - the same contract
    ``core.review_binding.invalidate_editorial_review_state()`` (Beta
    11.11B2B2) uses. Every query below runs against that one resolved alias.

    Issues exactly four read queries, regardless of how many translations or
    tags exist: the root prompt (its own ``author_id`` column - Beta
    11.11C4D dropped the ``select_related("author")`` join entirely, since
    no field on the related ``auth.User`` row is read anymore), the
    translations, the tags, and the tool ids. ``tools`` is a second,
    independent many-to-many relation that cannot be folded into the root
    query or the tags query without either an unbounded join or an extra
    round trip either way - so it gets its own single query rather than
    being fetched lazily per item. See the module docstring for why each of
    these four is in the payload and why nothing else is.
    """
    if prompt is None or not isinstance(prompt, Prompt):
        raise PromptReviewPayloadError(
            PromptReviewPayloadErrorCode.UNSUPPORTED_OBJECT,
            "build_prompt_review_payload() only accepts a prompts.Prompt instance, got "
            f"{'None' if prompt is None else type(prompt).__name__}",
        )

    if prompt.pk is None:
        raise PromptReviewPayloadError(
            PromptReviewPayloadErrorCode.UNSAVED_OBJECT,
            "build_prompt_review_payload() requires a saved object with a primary key",
        )

    if using is not None and not isinstance(using, str):
        raise TypeError(f"using must be a database alias string, got {type(using).__name__}")

    obj_db_alias = getattr(prompt._state, "db", None)

    # An unknown alias name is always INVALID_DATABASE_ALIAS, even if it also
    # happens to differ from obj_db_alias - "this alias does not exist" is the
    # more fundamental problem. DATABASE_ALIAS_MISMATCH is reserved for a
    # `using` that *is* a real, configured alias but conflicts with one the
    # object already claims to be bound to.
    if using is not None:
        if using not in connections:
            raise PromptReviewPayloadError(
                PromptReviewPayloadErrorCode.INVALID_DATABASE_ALIAS,
                f"{using!r} is not a configured database alias",
            )
        if obj_db_alias and obj_db_alias != using:
            raise PromptReviewPayloadError(
                PromptReviewPayloadErrorCode.DATABASE_ALIAS_MISMATCH,
                f"explicit using={using!r} does not match the object's own database "
                f"alias {obj_db_alias!r}",
            )
        db_alias = using
    else:
        db_alias = obj_db_alias or DEFAULT_DB_ALIAS
        if db_alias not in connections:
            raise PromptReviewPayloadError(
                PromptReviewPayloadErrorCode.INVALID_DATABASE_ALIAS,
                f"{db_alias!r} is not a configured database alias",
            )

    # Query 1: the root prompt row. No `select_related("author")` - the
    # payload only ever reads `author_id`, a plain column on this same row,
    # so joining in `auth_user` would be a pure, unused extra cost.
    try:
        root = Prompt._default_manager.using(db_alias).get(pk=prompt.pk)
    except Prompt.DoesNotExist:
        raise PromptReviewPayloadError(
            PromptReviewPayloadErrorCode.OBJECT_NOT_FOUND,
            f"prompts.Prompt #{prompt.pk} no longer exists",
        ) from None

    # Query 2: every stored translation row, sprachneutral - no
    # safe_translation_getter(), no fallback, no active-language dependency.
    translation_rows = list(
        PromptTranslation.objects.using(db_alias)
        .filter(master_id=root.pk)
        .values("language_code", *TRANSLATION_FIELDS)
    )
    translation_rows.sort(
        key=lambda row: (_normalize_language_code(row["language_code"]), row["language_code"])
    )
    translations = [_serialize_translation(row) for row in translation_rows]

    # Query 3: tag membership + the two public tag attributes, in one join
    # through taggit's own manager - never two separate queries.
    tag_rows = list(root.tags.using(db_alias).values("id", "slug", "name"))
    tag_rows.sort(key=lambda row: (row["slug"], row["name"], row["id"]))
    tags = [{"slug": row["slug"], "name": row["name"]} for row in tag_rows]

    # Query 4: linked tool ids only - membership, not Tool's own attributes.
    tool_ids = sorted(
        Tool.objects.using(db_alias).filter(prompts=root).values_list("pk", flat=True)
    )

    return {
        "schema": SCHEMA,
        "content_type": CONTENT_TYPE,
        "fields": {},
        "translations": translations,
        "relations": {
            "author": _serialize_author(root.author_id),
            "tools": tool_ids,
            "tags": tags,
        },
    }
