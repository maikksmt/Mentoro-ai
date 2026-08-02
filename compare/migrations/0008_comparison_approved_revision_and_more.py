"""
Beta 11.11B2A: add the review-binding columns to Comparison and clean up the
historical review/approved rows that can never carry a binding.

Schema
------
Three internal workflow columns from ``core.models.editorial``:
``review_revision`` and ``approved_revision`` (FKs to ``reversion.Revision``,
SET_NULL) and ``review_payload_fingerprint`` (CharField, default ``""``).

Data
----
Every comparison currently in ``review`` or ``approved`` was moved there before
any binding existed, so there is no provable answer to "which revision was
actually reviewed?". Beta 11.11A showed why guessing is not an option: the
content can have changed arbitrarily since the submit, and
``last_published_revision_id`` holds a ``Version.id`` describing a single
published row, not the reviewed graph. This migration therefore invents nothing
- it leaves all three new columns empty and returns the affected rows to a
state that requires a fresh, bindable review.

Comparison's live-state test is stricter than the other three editorial types,
because a comparison's public page is its parent text *and* its tool entries::

    has_live_snapshot = bool(live_i18n) and live_entries is not None

``live_entries`` carries the three-state contract Beta 11.9 introduced (see
``compare/models.py``): ``None`` means "published before the entry snapshot
existed", ``[]`` means "published with zero entries", and a filled list is the
ordinary case. Only the last two count as a complete published state:

* ``live_entries is None`` - a legacy record whose public page still reads its
  tool entries from the *live rows*. ``ComparisonQuerySet.visible_on_site()``
  already refuses to keep such a record online outside ``published`` for exactly
  that reason, so routing it to ``rework`` would claim a published state it
  cannot serve. It goes to ``draft``.
* ``live_entries == []`` - a real published snapshot that happens to have no
  entries. Testing truthiness instead of ``is not None`` would misread this as
  "never published" and send a genuinely published comparison to ``draft``,
  which is why the check is explicitly against ``None``.

``last_published_revision_id`` deliberately does not participate in the decision
in either direction. A row with the marker but no usable snapshot has nothing
published to show, so it goes to ``draft``. A row with a full snapshot but no
marker is still provably published content, so it goes to ``rework``.

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

    Reads just the three columns the decision needs and writes through two bulk
    ``update()`` calls. No ``save()`` anywhere, so no signals fire, no revision
    is created, and ``updated_at`` keeps its stored value instead of being
    rewritten by ``auto_now``. ``review_note`` is untouched - it can hold real
    reviewer feedback that this safety cleanup has no reason to destroy.
    """
    db_alias = schema_editor.connection.alias
    Comparison = apps.get_model("compare", "Comparison")

    to_draft = []
    to_rework = []
    rows = (
        Comparison.objects.using(db_alias)
        .filter(status__in=UNBOUND_STATUSES)
        .values_list("pk", "live_i18n", "live_entries")
        .iterator(chunk_size=500)
    )
    for pk, live_i18n, live_entries in rows:
        # bool(live_i18n) collapses NULL/{}/"" into "no parent snapshot".
        # live_entries is compared to None explicitly: [] is a valid published
        # snapshot with zero entries, not a missing one.
        has_live_snapshot = bool(live_i18n) and live_entries is not None
        (to_rework if has_live_snapshot else to_draft).append(pk)

    for pks, target_status in ((to_draft, "draft"), (to_rework, "rework")):
        if not pks:
            continue
        Comparison.objects.using(db_alias).filter(pk__in=pks).update(
            status=target_status, **CLEARED_WORKFLOW_METADATA
        )


class Migration(migrations.Migration):

    dependencies = [
        ("compare", "0007_comparison_live_entries"),
        ("reversion", "0002_add_index_on_version_for_content_type_and_db"),
    ]

    operations = [
        migrations.AddField(
            model_name="comparison",
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
            model_name="comparison",
            name="review_payload_fingerprint",
            field=models.CharField(
                blank=True, default="", editable=False, max_length=64
            ),
        ),
        migrations.AddField(
            model_name="comparison",
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
