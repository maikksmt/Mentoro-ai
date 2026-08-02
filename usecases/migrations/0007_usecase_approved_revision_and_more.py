"""
Beta 11.11B2A: add the review-binding columns to UseCase and clean up the
historical review/approved rows that can never carry a binding.

Schema
------
Three internal workflow columns from ``core.models.editorial``:
``review_revision`` and ``approved_revision`` (FKs to ``reversion.Revision``,
SET_NULL) and ``review_payload_fingerprint`` (CharField, default ``""``).

Data
----
Every use case currently in ``review`` or ``approved`` was moved there before any
binding existed, so there is no provable answer to "which revision was actually
reviewed?". Beta 11.11A showed why guessing is not an option: the content can
have changed arbitrarily since the submit, and ``last_published_revision_id``
holds a ``Version.id`` describing a single published row, not the reviewed
graph. This migration therefore invents nothing - it leaves all three new
columns empty and returns the affected rows to a state that requires a fresh,
bindable review:

* a provable live snapshot (``bool(live_i18n)``) -> ``rework``. The published
  snapshot stays authoritative and the page stays public, because
  ``EditorialQuerySet.visible_on_site()`` includes ``rework``.
* no provable live snapshot -> ``draft``. Fail closed: nothing that was never
  demonstrably published becomes visible through this migration.

``last_published_revision_id`` deliberately does not participate in that
decision in either direction. A row with the marker but an empty snapshot has
nothing published to show - the public surfaces would fall back to its *current
draft* text - so it goes to ``draft``. A row with a snapshot but no marker is
still provably published content, so it goes to ``rework``.

Reversibility
-------------
The schema half is reversible by Django. The data half is not, and pretends
nothing else: reverting cannot know which rows were ``review`` and which were
``approved``, and re-inventing those states would resurrect exactly the unbound
review the migration set out to remove. The reverse is therefore an explicit
no-op, and a downgrade leaves the cleaned statuses in place.
"""
import django.db.models.deletion
from django.db import migrations, models

#: Statuses whose historical rows carry no provable review binding.
UNBOUND_STATUSES = ("review", "approved")

#: Written to every cleaned row. The new columns are already NULL/"" from the
#: AddField operations above; setting them explicitly keeps the post-condition
#: independent of operation order and states the contract in one place.
CLEARED_WORKFLOW_METADATA = {
    "reviewed_by": None,
    "reviewed_at": None,
    "submitted_for_review_at": None,
    "review_revision": None,
    "approved_revision": None,
    "review_payload_fingerprint": "",
}


def clean_unbound_review_states(apps, schema_editor):
    """
    Historical models only, no runtime imports: no manager, no FSM transition,
    no snapshot builder, no reversion registration.

    Reads just the two columns the decision needs and writes through two bulk
    ``update()`` calls. No ``save()`` anywhere, so no signals fire, no revision
    is created, and ``updated_at`` keeps its stored value instead of being
    rewritten by ``auto_now``. ``review_note`` is untouched - it can hold real
    reviewer feedback that this safety cleanup has no reason to destroy.
    """
    db_alias = schema_editor.connection.alias
    UseCase = apps.get_model("usecases", "UseCase")

    to_draft = []
    to_rework = []
    rows = (
        UseCase.objects.using(db_alias)
        .filter(status__in=UNBOUND_STATUSES)
        .values_list("pk", "live_i18n")
        .iterator(chunk_size=500)
    )
    for pk, live_i18n in rows:
        # bool() collapses NULL, {} and "" into the same "no snapshot" answer.
        (to_rework if bool(live_i18n) else to_draft).append(pk)

    for pks, target_status in ((to_draft, "draft"), (to_rework, "rework")):
        if not pks:
            continue
        UseCase.objects.using(db_alias).filter(pk__in=pks).update(
            status=target_status, **CLEARED_WORKFLOW_METADATA
        )


class Migration(migrations.Migration):

    dependencies = [
        ("usecases", "0006_backfill_usecase_live_state"),
        ("reversion", "0002_add_index_on_version_for_content_type_and_db"),
    ]

    operations = [
        migrations.AddField(
            model_name="usecase",
            name="approved_revision",
            field=models.ForeignKey(
                blank=True,
                editable=False,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="reversion.revision",
            ),
        ),
        migrations.AddField(
            model_name="usecase",
            name="review_payload_fingerprint",
            field=models.CharField(
                blank=True, default="", editable=False, max_length=64
            ),
        ),
        migrations.AddField(
            model_name="usecase",
            name="review_revision",
            field=models.ForeignKey(
                blank=True,
                editable=False,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="reversion.revision",
            ),
        ),
        migrations.RunPython(
            clean_unbound_review_states,
            migrations.RunPython.noop,
        ),
    ]
