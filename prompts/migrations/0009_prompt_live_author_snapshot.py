"""
Beta 11.11C4E: add `Prompt.live_author` - a global (non-language-specific),
immutable snapshot of the public author display name, frozen at publish time
- and backfill it for every Prompt that already carries a content live
snapshot (`bool(live_i18n)`, the existing Beta 11.11B2A live-snapshot
contract), regardless of that row's *current* workflow status.

Why "any row with live_i18n", not "status == published"
---------------------------------------------------------
`EditorialQuerySet.LIVE_EDITING_STATUSES` (`review`, `approved`, `rework`)
already keeps a previously-published page online while it goes through
another editorial round - its public presence depends on the *snapshot*
existing, not on the current workflow status. An archived row can also still
carry a stale-but-once-real `live_i18n`. Backfilling only `status ==
"published"` would silently leave every rework/approved/archived row that a
future rendering slice reads `live_author` from without one. Scoping to
`bool(live_i18n)` mirrors the exact rule
`core.review_binding.has_provable_live_snapshot()` already uses for Prompt,
reproduced locally below rather than imported (see "No runtime imports").

Historical accuracy boundary
------------------------------
For a row that was published before this migration existed, the display name
actually shown to the public at the moment of that original publish was never
separately recorded anywhere - `live_i18n` holds content, not the author's
name, and no prior slice stored one. This backfill therefore freezes the
*currently* resolved name (`get_full_name() or username or ""`) as of the
moment this migration runs, not the name that may have been displayed back
then. If the account's name changed since the original publish, the
backfilled snapshot can differ from history - this is a one-time,
unavoidable approximation, not a defect: the project held no immutable name
history before this slice, so there is nothing more accurate to recover.
After this backfill, the normal runtime contract applies from then on: a
snapshot changes only through a conscious republish.

No runtime imports
--------------------
Nothing here imports `prompts.models`, `prompts.review_payload`,
`prompts.review_submission`, `prompts.review_approval`,
`core.models.editorial`, `core.review_binding`, or `core.review_invalidation`
- only `apps.get_model(...)` historical models and the standard library. The
snapshot shape (`{"schema": "prompt-author-v1", "display_name": ...}`) and
the name-resolution formula are reproduced locally, frozen at exactly what
they were when this migration was written; a later change to
`Prompt._build_live_author_snapshot()` must never change what this migration
does. A parity test (`prompts/tests/test_author_snapshot_migration.py`)
confirms the two independently agree for representative rows as of this
slice - it does not run as part of the migration itself.

No reversion revisions or versions are created: every write is a targeted,
per-row `QuerySet.update()` (never `.save()`), so no `post_save` signal ever
fires and `updated_at` is never touched (`auto_now` only fires through
`Model.save()`).

Idempotent: a row whose `live_author` is already an exact, well-formed
`prompt-author-v1` snapshot (both keys present, nothing else, `schema`
matching, `display_name` a string) is left completely untouched on a repeat
run. Only a missing or malformed value on a row with `live_i18n` still set is
(re)computed.

Reversible
-----------
`AddField` reverses to `RemoveField`, which drops the column outright - so
there is nothing left for the data half to undo, and its `reverse_code` is a
real, honest `RunPython.noop` here (never a stand-in for genuinely
irreversible logic the way one would be for
`prompts/migrations/0008_prompt_review_payload_v2.py`, which does not use one
at all and explains why in its own module docstring): once this migration is
reversed, `live_author` does not exist as a column, and any prior value was
never anything other than a currently-recomputable snapshot outside of a
separately-versioned history.
"""
from django.db import migrations, models

PROMPT_AUTHOR_SNAPSHOT_SCHEMA = "prompt-author-v1"


def _is_valid_v1_snapshot(value):
    return (
        isinstance(value, dict)
        and set(value) == {"schema", "display_name"}
        and value.get("schema") == PROMPT_AUTHOR_SNAPSHOT_SCHEMA
        and isinstance(value.get("display_name"), str)
    )


def _build_snapshot(*, has_author, first_name, last_name, username):
    if not has_author:
        return {"schema": PROMPT_AUTHOR_SNAPSHOT_SCHEMA, "display_name": ""}
    full_name = f"{first_name or ''} {last_name or ''}".strip()
    display_name = full_name or username or ""
    return {"schema": PROMPT_AUTHOR_SNAPSHOT_SCHEMA, "display_name": display_name}


def backfill_live_author_snapshots(apps, schema_editor):
    alias = schema_editor.connection.alias
    Prompt = apps.get_model("prompts", "Prompt")

    rows = (
        Prompt.objects.using(alias)
        .order_by("pk")
        .values(
            "pk",
            "live_i18n",
            "live_author",
            "author_id",
            "author__first_name",
            "author__last_name",
            "author__username",
        )
    )
    for row in rows:
        if not row["live_i18n"]:
            continue  # no content live snapshot - live_author stays None
        if _is_valid_v1_snapshot(row["live_author"]):
            continue  # already migrated - idempotent no-op

        snapshot = _build_snapshot(
            has_author=row["author_id"] is not None,
            first_name=row["author__first_name"],
            last_name=row["author__last_name"],
            username=row["author__username"],
        )
        Prompt.objects.using(alias).filter(pk=row["pk"]).update(live_author=snapshot)


class Migration(migrations.Migration):

    dependencies = [
        ("prompts", "0008_prompt_review_payload_v2"),
    ]

    operations = [
        migrations.AddField(
            model_name="prompt",
            name="live_author",
            field=models.JSONField(blank=True, default=None, null=True),
        ),
        migrations.RunPython(
            backfill_live_author_snapshots,
            migrations.RunPython.noop,
        ),
    ]
