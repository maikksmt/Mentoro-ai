"""
Beta 11.7A: make existing published use cases compatible with the Beta 11.7
public-visibility contract, without republishing anything by hand.

Two legacy gaps are closed, both purely in data:

1. ``last_published_revision_id`` - Beta 11.7 made this the marker that keeps
   a previously published use case public while it sits in an editing state
   (review/approved/rework). Records published before the marker was reliably
   written carry NULL and would silently drop off the public site on deploy.

2. ``persona`` inside ``live_i18n`` - Beta 11.7 added persona to
   LIVE_SNAPSHOT_FIELDS because the public card renders it. Snapshots written
   earlier have no ``persona`` key, so the card would render nothing for them
   until someone republished each use case individually.

Both are reconstructed from django-reversion, never invented. Anything that
cannot be established from the real recorded history aborts the migration
(fail closed) rather than guessing - a wrong marker would publish an
unverified state, and a wrong persona would publish draft text.
"""
import copy
import json

from django.db import migrations

#: Snapshot key this migration backfills.
PERSONA_KEY = "persona"


def _deserialize(raw):
    """The single object payload reversion stores per Version."""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, list) or not payload:
        return None
    fields = payload[0].get("fields")
    return fields if isinstance(fields, dict) else None


def _as_snapshot(value):
    """``live_i18n`` is a dict on the model but arrives as text in some payloads."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def _find_publication_version(versions_oldest_first, live_snapshot):
    """
    The recorded version at which the snapshot now serving the public site
    was published, or None.

    Three signals must agree, which is what makes this safe:

    * the serialized ``live_i18n`` equals the snapshot in the database, so
      this version *is* the state the public pages render today;
    * the serialized ``is_published`` is true, so it was recorded in a
      published state rather than mid-draft; and
    * the immediately preceding version carried a *different* ``live_i18n``
      (or there is none), so this version is the moment
      ``_update_live_snapshot()`` actually ran - the publication itself.

    The third condition is what makes this usable as the persona source.
    Editing a published use case produces further revisions that still carry
    the same ``live_i18n`` and still say ``is_published`` - only their
    translation rows hold the new draft text. Picking the newest matching
    version would therefore read the draft persona out of a revision that
    merely *follows* the publication. Anchoring on the transition instead
    yields the revision whose translations are the published ones.

    Where a use case was published, edited and republished to the very same
    values, several transitions match; the latest is returned, i.e. the most
    recent publication of this exact snapshot.

    Deliberately NOT used: the newest version regardless of content (that is
    the current draft), any version merely older than ``published_at``, or
    the workflow status on its own.
    """
    found = None
    previous_snapshot = None
    first = True

    for version in versions_oldest_first:
        fields = _deserialize(version.serialized_data)
        if not fields:
            continue
        current_snapshot = _as_snapshot(fields.get("live_i18n"))

        changed_here = first or current_snapshot != previous_snapshot
        if (
            changed_here
            and fields.get("is_published")
            and current_snapshot == live_snapshot
        ):
            found = version

        previous_snapshot = current_snapshot
        first = False

    return found


def _personas_from_revision(TranslationVersionModel, revision_id, translation_ct_id, use_case_id):
    """
    ``{language_code: persona}`` as recorded in the same revision, scoped to
    this use case's own translations.

    reversion registers UseCase with ``follow=("translations",)``, so the
    publishing revision holds the translation rows alongside the use case -
    these are the persona values that were live when that snapshot was
    published.
    """
    personas = {}
    if not revision_id:
        return personas
    rows = TranslationVersionModel.objects.filter(
        revision_id=revision_id, content_type_id=translation_ct_id
    )
    for row in rows:
        fields = _deserialize(row.serialized_data)
        if not fields:
            continue
        if str(fields.get("master")) != str(use_case_id):
            continue
        language_code = fields.get("language_code")
        if language_code and PERSONA_KEY in fields:
            personas[language_code] = fields[PERSONA_KEY]
    return personas


def backfill_live_state(apps, schema_editor):
    UseCase = apps.get_model("usecases", "UseCase")
    UseCaseTranslation = apps.get_model("usecases", "UseCaseTranslation")
    Version = apps.get_model("reversion", "Version")
    ContentType = apps.get_model("contenttypes", "ContentType")

    db_alias = schema_editor.connection.alias

    use_case_ct = ContentType.objects.using(db_alias).filter(
        app_label="usecases", model="usecase"
    ).first()
    translation_ct = ContentType.objects.using(db_alias).filter(
        app_label="usecases", model="usecasetranslation"
    ).first()

    archived = "archived"

    examined = 0
    markers_set = 0
    persona_from_revision = 0
    persona_from_current = 0
    unchanged = 0
    unresolved_marker_ids = []
    unresolved_persona = []

    queryset = (
        UseCase.objects.using(db_alias)
        .exclude(live_i18n={})
        .exclude(live_i18n__isnull=True)
        .order_by("pk")
    )

    for use_case in queryset.iterator(chunk_size=100):
        snapshot = use_case.live_i18n
        if not isinstance(snapshot, dict) or not snapshot:
            continue
        examined += 1

        versions = []
        if use_case_ct is not None:
            versions = list(
                Version.objects.using(db_alias)
                .filter(content_type_id=use_case_ct.pk, object_id=str(use_case.pk))
                .order_by("pk")
            )
        publication = _find_publication_version(versions, snapshot)

        update_fields = []

        # --- 1. the live-revision marker -----------------------------------
        needs_marker = use_case.last_published_revision_id is None
        if needs_marker and use_case.status != archived:
            # Archived use cases are excluded on purpose: archiving is the
            # deliberate public withdrawal, so visible_on_site() hides them
            # whatever the marker says. Demanding a provable publication
            # version for a record that can never go public again would block
            # the deploy for no gain.
            if publication is None:
                unresolved_marker_ids.append(use_case.pk)
            else:
                use_case.last_published_revision_id = publication.pk
                update_fields.append("last_published_revision_id")
                markers_set += 1

        # --- 2. persona inside each existing language snapshot --------------
        revision_personas = _personas_from_revision(
            Version,
            getattr(publication, "revision_id", None),
            translation_ct.pk if translation_ct is not None else None,
            use_case.pk,
        )

        new_snapshot = copy.deepcopy(snapshot)
        snapshot_changed = False

        for language_code, values in sorted(new_snapshot.items()):
            if not isinstance(values, dict):
                continue
            if PERSONA_KEY in values:
                # Never overwrite a value that is already published.
                continue

            if language_code in revision_personas:
                values[PERSONA_KEY] = revision_personas[language_code] or ""
                persona_from_revision += 1
                snapshot_changed = True
                continue

            # Compatibility fallback, explicitly limited to persona and to the
            # same language: before Beta 11.7 the public card rendered exactly
            # this current value (templates/usecases/list.html read
            # obj.persona directly), so adopting it changes nothing a visitor
            # already saw. No other field may be sourced this way, and no
            # other language may stand in.
            current = (
                UseCaseTranslation.objects.using(db_alias)
                .filter(master_id=use_case.pk, language_code=language_code)
                .values_list(PERSONA_KEY, flat=True)
                .first()
            )
            if current is not None:
                values[PERSONA_KEY] = current or ""
                persona_from_current += 1
                snapshot_changed = True
                continue

            unresolved_persona.append((use_case.pk, language_code))

        if snapshot_changed:
            use_case.live_i18n = new_snapshot
            update_fields.append("live_i18n")

        if update_fields:
            # Only the two fields this migration owns. Saving with
            # update_fields keeps status, timestamps (updated_at is auto_now,
            # so it must never be part of a save here), relations and every
            # draft translation untouched, and writing through the historical
            # model means no signal, no admin hook and no new revision.
            UseCase.objects.using(db_alias).filter(pk=use_case.pk).update(
                **{name: getattr(use_case, name) for name in update_fields}
            )
        else:
            unchanged += 1

    if unresolved_marker_ids or unresolved_persona:
        raise RuntimeError(
            "usecases.0006_backfill_usecase_live_state cannot establish the "
            "published live state for every use case and refuses to guess, "
            "because an invented marker would publish an unverified revision "
            "and an invented persona would publish draft text.\n"
            f"  use cases with a live snapshot but no provable publication "
            f"version (ids): {sorted(unresolved_marker_ids)}\n"
            f"  language snapshots with no persona in either the publication "
            f"revision or the current translation (id, language): "
            f"{sorted(unresolved_persona)}\n"
            "Resolve by republishing the listed use cases through the normal "
            "editorial workflow (which writes both values), then re-run "
            "migrate."
        )

    # Counts only - no titles, slugs, persona values or user data are logged.
    print(
        "  usecases.0006 backfill: "
        f"examined={examined} "
        f"markers_set={markers_set} "
        f"persona_from_revision={persona_from_revision} "
        f"persona_compat_fallback={persona_from_current} "
        f"unchanged={unchanged}"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("usecases", "0005_alter_usecase_updated_at"),
        ("reversion", "0001_squashed_0004_auto_20160611_1202"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        # Reverse is a no-op on purpose: the forward direction only fills in
        # values that were missing, and there is no safe way to tell a
        # backfilled value from one an editor published afterwards.
        migrations.RunPython(backfill_live_state, migrations.RunPython.noop),
    ]
