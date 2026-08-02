"""
Beta 11.11C4D: migrate existing Prompt review/approval bindings from the
``prompt-review-v1`` payload contract onto ``prompt-review-v2``.

Beta 11.11C4C's audit found that the v1 payload's author section -
``{"username": ..., "display_name": ...}``, computed live from the bound
``auth.User`` row - made a pure display-name or username change (never a
redactional content change) alter the review fingerprint. Beta 11.11C4D's
runtime change (``prompts/review_payload.py``) narrows the author section to
``{"id": author_id}``: only a real author reassignment changes the payload
now.

That runtime change alone would silently break every prompt already bound
under v1: its stored ``review_payload_fingerprint`` was computed against the
old shape and would no longer match *anything* the new builder can produce,
even for content that never changed. This migration is the one-time,
historically frozen bridge - not a general-purpose reusable primitive, and
deliberately not the real ``prompts.review_payload``/``prompts.review_submission``/
``prompts.review_approval``/``core.review_binding`` modules, which must stay
free to change without silently rewriting this migration's behaviour.

For every Prompt currently ``review`` or ``approved`` (the only statuses that
can carry a provable binding - see Beta 11.11B2A/B2B1/B2B2), locked one at a
time in primary-key order:

1. Validate the binding structure using a local reimplementation of the exact
   ``core.review_binding.validate_review_binding``/``validate_approved_binding``
   rules (revision set, fingerprint syntactically valid, revision actually
   contains this prompt's root version; for ``approved`` rows additionally
   ``approved_revision_id == review_revision_id`` and that revision also
   contains the root) - plus the one pre-state check Beta 11.11C3A added on
   top of the central validator: a ``review``-status row must not already
   carry an ``approved_revision``.
2. If structurally invalid: fail-closed invalidate (see below). No content
   comparison is meaningful against a binding that was never valid to begin
   with.
3. If structurally valid: build the frozen historical v1 payload *and* the
   frozen v2 payload for the row's *current* stored content, fingerprint
   both, and classify the stored fingerprint against them, in this order:

   * ``stored == current_v2`` -> already migrated; a pure no-op (idempotent -
     a second run of this migration, or a row a previous partial run already
     reached, changes nothing).
   * ``stored == current_v1`` -> the content has demonstrably not changed
     since submit under the contract that was in force at submit time; only
     ``review_payload_fingerprint`` is updated to the v2 value. Nothing else
     changes: not the status, not either revision FK, not the reviewer
     fields, not ``updated_at`` (this is a pure administrative fingerprint
     relabelling, not a content or workflow event).
   * neither matches -> fail-closed invalidate.

Why a fingerprint mismatch is invalidated rather than assumed safe
--------------------------------------------------------------------
A stored fingerprint that matches neither the current v1 nor the current v2
payload could mean the translation, tag or tool state changed since submit -
or it could, in principle, mean *only* the author's display name changed
before this migration ran (the one thing v1 fingerprinted that v2 no longer
does). Beta 11.11B1 never added tags, tools, or the author's account fields
to the reversion follow-graph, and C1 never stored the original payload
separately - so, for a one-time historical row, these two causes are not
distinguishable after the fact. Rather than assume the more charitable
explanation, this migration invalidates: exactly the same fail-closed
principle Beta 11.11B2A and every submit/approval primitive since have used
throughout this series. This is a one-time migration boundary, not a new
runtime contract - once a row is on v2, a later author display-name change
never triggers this again (the v2 payload no longer contains it at all).

Invalidation itself reproduces the exact Beta 11.11B2B2 data contract
(``core.review_binding.invalidate_editorial_review_state``): target status is
``rework`` if the row has a provable live snapshot (``bool(live_i18n)``,
Prompt's exact B2A contract), otherwise ``draft``; the six binding/reviewer
columns are cleared; everything else - content, translations, tags, tools,
``author_id``, ``review_note``, ``live_i18n``, ``last_published_revision_id``,
``published_at``, ``is_published`` - is left untouched. ``updated_at`` is
explicitly bumped only in this branch, exactly as the real primitive's own
``save(update_fields=[...])`` would via ``auto_now`` - never in the
fingerprint-relabelling branch above, since ``QuerySet.update()`` does not
implicitly touch ``auto_now`` fields the way ``Model.save()`` does.

Deliberately not migrated: ``draft``, ``rework``, ``published`` and
``archived`` rows are never selected in the first place - they carry no
provable binding today (draft/rework structurally cannot; published/archived
retain whatever binding they had, which this slice's product decision left
untouched pending a future publish-guard slice - see the Beta 11.11C4D
closing report).

No runtime imports
-------------------
This migration imports nothing from ``prompts.models``, ``prompts.review_payload``,
``prompts.review_submission``, ``prompts.review_approval``,
``core.review_binding``, or ``core.review_invalidation`` - only
``apps.get_model(...)`` historical models, the standard library, and Django's
own migration/ORM/timezone APIs. Every payload-shape, fingerprint, and
binding-validation rule needed is reproduced locally below, frozen at exactly
what it was when this migration was written. A later change to the real
runtime modules must never change what this migration does when it (or a
copy of its ``RunPython`` function, run again for verification) executes.

No reversion revisions or versions are created: every write below is a bulk
``QuerySet.update()``, which never calls ``Model.save()`` and therefore never
fires the ``post_save`` signal reversion's recorder listens for.

Irreversible
------------
There is no reverse operation. Reconstructing which rows were ``review`` vs.
``approved``, or re-fingerprinting under the now-superseded v1 contract, would
require information (the original v1 payload, prior to whatever the current
row now looks like) this migration does not keep - and pretending otherwise
would resurrect exactly the unbound-review ambiguity Beta 11.11A originally
flagged. Migrating backwards past this point raises Django's own
``IrreversibleError``.
"""
import hashlib
import json

from django.db import migrations, transaction

#: The exact two statuses that can carry a provable review/approval binding
#: today (Beta 11.11B2A/B2B1/B2B2). Every other status is left completely
#: alone - not read, not locked, not written.
_MIGRATABLE_STATUSES = ("review", "approved")

#: Beta 11.11C1's original whitelist of `PromptTranslation` fields, in the
#: exact serialization order both the v1 and v2 payload shapes still use.
_TRANSLATION_FIELDS = ("title", "intro", "body", "outro", "slug", "public_slug")

_LOWERCASE_HEX_DIGITS = frozenset("0123456789abcdef")


# ======================================================================
# Frozen fingerprint primitives (core.review_binding, reproduced verbatim)
# ======================================================================


def _fingerprint(payload):
    """Byte-for-byte reproduction of ``core.review_binding.fingerprint_review_payload``:
    the payloads this migration ever builds are constructed entirely from
    JSON-safe scalars, so the defensive type-rejection pass that function
    also performs is not needed here to get an identical digest."""
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_valid_sha256_hexdigest(value):
    """Reproduction of ``core.review_binding._is_valid_sha256_hexdigest``."""
    if not isinstance(value, str):
        return False
    if len(value) != 64:
        return False
    return all(char in _LOWERCASE_HEX_DIGITS for char in value)


def _revision_contains_prompt_root(Version, alias, revision_id, prompt_pk):
    """
    Reproduction of ``core.review_binding.revision_contains_object``, narrowed
    to exactly the one concrete type this migration ever needs:
    ``prompts.prompt``. No ``ContentType.objects.get_for_model()`` cache
    touch - a plain join filter through the public ``content_type__*``
    relation fields, exactly like the real function.
    """
    if revision_id is None:
        return False
    return (
        Version.objects.using(alias)
        .filter(
            revision_id=revision_id,
            content_type__app_label="prompts",
            content_type__model="prompt",
            object_id=str(prompt_pk),
            db=alias,
        )
        .exists()
    )


# ======================================================================
# Frozen binding validation (core.review_binding, reproduced verbatim)
# ======================================================================


def _review_binding_reason(locked, Version, alias):
    """Reproduction of ``core.review_binding.validate_review_binding``'s
    fixed check order. Returns ``None`` when structurally valid."""
    if locked.review_revision_id is None:
        return "review_revision_missing"
    fingerprint = locked.review_payload_fingerprint
    if not fingerprint:
        return "review_fingerprint_missing"
    if not _is_valid_sha256_hexdigest(fingerprint):
        return "review_fingerprint_invalid"
    if not _revision_contains_prompt_root(Version, alias, locked.review_revision_id, locked.pk):
        return "review_revision_not_for_object"
    return None


def _approved_binding_reason(locked, Version, alias):
    """Reproduction of ``core.review_binding.validate_approved_binding``'s
    fixed check order (which itself starts from the review-binding check)."""
    reason = _review_binding_reason(locked, Version, alias)
    if reason is not None:
        return reason
    if locked.approved_revision_id is None:
        return "approved_revision_missing"
    if locked.approved_revision_id != locked.review_revision_id:
        return "approved_revision_mismatch"
    if not _revision_contains_prompt_root(Version, alias, locked.approved_revision_id, locked.pk):
        return "approved_revision_not_for_object"
    return None


def _binding_reason(locked, Version, alias):
    """
    Dispatches on the row's own (locked, current) status. For ``review``,
    additionally reproduces the one pre-state check Beta 11.11C3A added on
    top of the central validator - a ``review`` row must not already carry an
    ``approved_revision`` - which the shared structural checks above
    deliberately do not cover.
    """
    if locked.status == "review":
        reason = _review_binding_reason(locked, Version, alias)
        if reason is None and locked.approved_revision_id is not None:
            return "approved_revision_already_set_in_review_status"
        return reason
    return _approved_binding_reason(locked, Version, alias)


# ======================================================================
# Frozen payload builders (prompts.review_payload, reproduced verbatim)
# ======================================================================


def _normalize_language_code(language_code):
    return language_code.strip().lower()


def _serialize_translation(row):
    return {
        "language_code": row["language_code"],
        **{name: row[name] for name in _TRANSLATION_FIELDS},
    }


def _translations_for(PromptTranslation, alias, prompt_pk):
    rows = list(
        PromptTranslation.objects.using(alias)
        .filter(master_id=prompt_pk)
        .values("language_code", *_TRANSLATION_FIELDS)
    )
    rows.sort(key=lambda row: (_normalize_language_code(row["language_code"]), row["language_code"]))
    return [_serialize_translation(row) for row in rows]


def _tags_for(TaggedItem, Tag, alias, prompt_pk):
    tag_ids = list(
        TaggedItem.objects.using(alias)
        .filter(content_type__app_label="prompts", content_type__model="prompt", object_id=prompt_pk)
        .values_list("tag_id", flat=True)
    )
    rows = list(Tag.objects.using(alias).filter(pk__in=tag_ids).values("id", "slug", "name"))
    rows.sort(key=lambda row: (row["slug"], row["name"], row["id"]))
    return [{"slug": row["slug"], "name": row["name"]} for row in rows]


def _tool_ids_for(locked, alias):
    return sorted(locked.tools.using(alias).values_list("pk", flat=True))


def _serialize_author_v1(user):
    """
    Frozen reproduction of the pre-C4D ``prompts.review_payload._serialize_author``.
    ``user`` is a historical ``auth.User`` model instance (or ``None``) - it
    has no ``get_full_name()`` method (historical models reconstruct fields,
    not custom methods), so Django's own formula is inlined instead.
    """
    if user is None:
        return None
    full_name = ("%s %s" % (user.first_name, user.last_name)).strip()
    display_name = full_name or user.username or ""
    return {"username": user.username, "display_name": display_name}


def _serialize_author_v2(author_id):
    """Frozen reproduction of the post-C4D ``prompts.review_payload._serialize_author``."""
    if author_id is None:
        return None
    return {"id": author_id}


def _build_payload(schema, locked, PromptTranslation, TaggedItem, Tag, alias, author_section):
    return {
        "schema": schema,
        "content_type": "prompt",
        "fields": {},
        "translations": _translations_for(PromptTranslation, alias, locked.pk),
        "relations": {
            "author": author_section,
            "tools": _tool_ids_for(locked, alias),
            "tags": _tags_for(TaggedItem, Tag, alias, locked.pk),
        },
    }


def _build_v1_payload(locked, PromptTranslation, TaggedItem, Tag, alias):
    return _build_payload(
        "prompt-review-v1", locked, PromptTranslation, TaggedItem, Tag, alias,
        _serialize_author_v1(locked.author),
    )


def _build_v2_payload(locked, PromptTranslation, TaggedItem, Tag, alias):
    return _build_payload(
        "prompt-review-v2", locked, PromptTranslation, TaggedItem, Tag, alias,
        _serialize_author_v2(locked.author_id),
    )


# ======================================================================
# Frozen invalidation (core.review_binding.invalidate_editorial_review_state,
# narrowed to Prompt's own live-snapshot contract)
# ======================================================================


def _target_status(locked):
    """Prompt's exact Beta 11.11B2A live-snapshot rule:
    ``bool(obj.live_i18n)`` -> ``rework``, else -> ``draft``."""
    return "rework" if bool(locked.live_i18n) else "draft"


def _invalidate(Prompt, alias, locked, now):
    Prompt.objects.using(alias).filter(pk=locked.pk).update(
        status=_target_status(locked),
        review_revision=None,
        approved_revision=None,
        review_payload_fingerprint="",
        reviewed_by=None,
        reviewed_at=None,
        submitted_for_review_at=None,
        updated_at=now,
    )


# ======================================================================
# Per-row migration and entry point
# ======================================================================


def _migrate_one(Prompt, PromptTranslation, TaggedItem, Tag, Version, alias, pk, now):
    # `of=("self",)` restricts the row lock to the prompt table itself:
    # `author` is nullable, so the plain `select_related("author")` join is a
    # LEFT OUTER JOIN, and PostgreSQL refuses `FOR UPDATE` on the nullable
    # side of an outer join. The lock was only ever meant to serialize
    # concurrent writers of this Prompt row, not of `auth_user`.
    locked = (
        Prompt.objects.using(alias)
        .select_for_update(of=("self",))
        .select_related("author")
        .get(pk=pk)
    )

    reason = _binding_reason(locked, Version, alias)
    if reason is not None:
        _invalidate(Prompt, alias, locked, now)
        return

    payload_v2 = _build_v2_payload(locked, PromptTranslation, TaggedItem, Tag, alias)
    fingerprint_v2 = _fingerprint(payload_v2)
    stored = locked.review_payload_fingerprint

    if stored == fingerprint_v2:
        return  # already migrated - idempotent no-op

    payload_v1 = _build_v1_payload(locked, PromptTranslation, TaggedItem, Tag, alias)
    fingerprint_v1 = _fingerprint(payload_v1)

    if stored == fingerprint_v1:
        # Demonstrably unchanged since submit under the v1 contract - relabel
        # the fingerprint only. Bare QuerySet.update(): no auto_now bump, no
        # save(), no signal, no revision.
        Prompt.objects.using(alias).filter(pk=pk).update(review_payload_fingerprint=fingerprint_v2)
        return

    _invalidate(Prompt, alias, locked, now)


def migrate_prompt_review_payload_to_v2(apps, schema_editor):
    from django.utils import timezone

    alias = schema_editor.connection.alias
    Prompt = apps.get_model("prompts", "Prompt")
    PromptTranslation = apps.get_model("prompts", "PromptTranslation")
    TaggedItem = apps.get_model("taggit", "TaggedItem")
    Tag = apps.get_model("taggit", "Tag")
    Version = apps.get_model("reversion", "Version")

    now = timezone.now()

    with transaction.atomic(using=alias):
        pks = list(
            Prompt.objects.using(alias)
            .filter(status__in=_MIGRATABLE_STATUSES)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        for pk in pks:
            _migrate_one(Prompt, PromptTranslation, TaggedItem, Tag, Version, alias, pk, now)


class Migration(migrations.Migration):

    dependencies = [
        ("prompts", "0007_prompt_approved_revision_and_more"),
        ("reversion", "0002_add_index_on_version_for_content_type_and_db"),
        ("taggit", "0006_rename_taggeditem_content_type_object_id_taggit_tagg_content_8fc721_idx"),
    ]

    operations = [
        migrations.RunPython(migrate_prompt_review_payload_to_v2),
    ]
