"""
Shared builders for the Beta 11.9 comparison live-snapshot tests.

Mirrors ``usecases/tests/live_visibility_fixtures.py`` (Beta 11.7), extended
by the tool-entry choreography Comparison needs.

Two details matter and are handled here:

* ``publish()`` alone does not set ``last_published_revision_id`` - the
  admin does that via ``core.admin.set_last_published_revision()``, which
  reads the newest reversion ``Version``. ``visible_on_site()`` requires the
  marker for review/approved/rework objects, so :func:`publish` sets it
  explicitly, exactly as every Guide/Prompt/UseCase test does.
* After publishing, editing a translation or a tool entry only changes the
  draft rows - ``live_i18n`` and ``live_entries`` keep the published state.
  That divergence is the whole point.
"""
from django.contrib.auth import get_user_model
from django.utils import timezone

from catalog.models import Tool
from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin

User = get_user_model()


def make_user(username):
    return User.objects.create_user(username=username, password="pw", is_staff=True)


def make_tool(slug, name=None, language="en", published_at=None):
    """``published_at=None`` leaves the model default (now) in place - only
    pass a value to build a not-yet-public or scheduled tool."""
    kwargs = {"slug": slug, "website": f"https://example.com/{slug}"}
    if published_at is not None:
        kwargs["published_at"] = published_at
    tool = Tool.objects.create(**kwargs)
    tool.create_translation(language, name=name or slug)
    return tool


def make_comparison(*, slug, title, intro="i", body="b", language="en", author=None):
    """A comparison that was never published: no snapshots at all."""
    comparison = Comparison.objects.create(
        author=author, status=EditorialWorkflowMixin.STATUS_DRAFT
    )
    comparison.create_translation(language, title=title, intro=intro, body=body, slug=slug)
    return comparison


def add_translation(comparison, language, *, slug, title, intro="i", body="b"):
    comparison.create_translation(language, title=title, intro=intro, body=body, slug=slug)
    return comparison


def add_entry(comparison, tool, *, position, language="en", label="", summary="",
              pros="", cons="", special=""):
    entry = comparison.tool_entries.create(tool=tool, position=position)
    entry.create_translation(
        language, label=label, summary=summary, pros=pros, cons=cons, special=special
    )
    return entry


def add_entry_translation(entry, language, *, label="", summary="", pros="", cons="",
                          special=""):
    entry.create_translation(
        language, label=label, summary=summary, pros=pros, cons=cons, special=special
    )
    return entry


def publish(comparison, by):
    """
    Run the real FSM publish so ``live_i18n`` and ``live_entries`` are
    written by the real ``on_after_publish()``, then set the live-revision
    marker the admin would set (see module docstring).
    """
    if comparison.status != EditorialWorkflowMixin.STATUS_APPROVED:
        if comparison.status not in (
            EditorialWorkflowMixin.STATUS_REVIEW,
            EditorialWorkflowMixin.STATUS_APPROVED,
        ):
            comparison.move_to_review(by=by)
            comparison.save()
        comparison.approve(by=by)
        comparison.save()
    comparison.publish(by=by)
    if not comparison.published_at:
        comparison.published_at = timezone.now()
    comparison.save()

    fresh = Comparison.objects.get(pk=comparison.pk)
    fresh.last_published_revision_id = 1
    fresh.save(update_fields=["last_published_revision_id"])
    return fresh


def save_draft_edit(comparison, language, **fields):
    """
    Write new values into the *current* translation row only.

    Re-reads the row first, exactly as the admin change form does: publishing
    writes the snapshots onto a different instance, so saving a stale
    in-memory object here would write them back and destroy the divergence
    these tests are about.
    """
    fresh = Comparison.objects.get(pk=comparison.pk)
    fresh.set_current_language(language)
    for name, value in fields.items():
        setattr(fresh, name, value)
    fresh.save()
    return fresh


def save_entry_draft_edit(entry, language, **fields):
    """Edit a tool entry's saved translation without publishing."""
    fresh = entry.__class__.objects.get(pk=entry.pk)
    fresh.set_current_language(language)
    for name, value in fields.items():
        setattr(fresh, name, value)
    fresh.save()
    return fresh


def start_review_round(comparison, by):
    """
    Move a published comparison back into review, exactly as the admin's
    auto-review guard does when an author saves a change.
    """
    fresh = Comparison.objects.get(pk=comparison.pk)
    fresh.move_to_review(by=by)
    fresh.save()
    return Comparison.objects.get(pk=fresh.pk)


def request_rework(comparison, by):
    """Editor sends the new draft back: review -> rework."""
    fresh = Comparison.objects.get(pk=comparison.pk)
    fresh.request_rework(by=by, note="")
    fresh.save()
    return Comparison.objects.get(pk=fresh.pk)


def archive(comparison, by):
    fresh = Comparison.objects.get(pk=comparison.pk)
    fresh.archive(by=by)
    fresh.is_published = False
    fresh.save()
    return Comparison.objects.get(pk=fresh.pk)


def make_legacy_published(comparison, by):
    """
    A record published before Beta 11.9: parent snapshot present, entry
    snapshot never written (``live_entries IS NULL``).
    """
    published = publish(comparison, by)
    Comparison.objects.filter(pk=published.pk).update(live_entries=None)
    return Comparison.objects.get(pk=published.pk)
