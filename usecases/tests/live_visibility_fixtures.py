"""
Shared builders for the Beta 11.7 use-case live-visibility tests.

Mirrors ``guides/tests/draft_preview_fixtures.py`` (Beta 11.4): one small
helper module so the focused test modules stay readable without repeating
the publish-then-edit choreography.

Two details matter and are handled here:

* ``publish()`` alone does not set ``last_published_revision_id`` - the
  admin does that via ``core.admin.set_last_published_revision()``, which
  reads the newest reversion ``Version``. ``visible_on_site()`` requires the
  marker for review/approved objects, so :func:`publish` sets it explicitly,
  exactly as every Guide/Prompt test does ("simulates reversion pointing at
  the live version", see guides/tests/test_live_revisions.py).
* After publishing, editing a translation only changes the translation row -
  ``live_i18n`` keeps the published snapshot. That divergence is the whole
  point: it is what the public surfaces must keep hiding.
"""
from django.contrib.auth import get_user_model
from django.utils import timezone

from core.models.editorial import EditorialWorkflowMixin
from usecases.models import UseCase

User = get_user_model()

#: Values that must only ever appear once actually published.
DRAFT_MARKER = "Draft Only Marker Text"
#: Values that were published and must stay visible through a review round.
LIVE_MARKER = "Live Snapshot Marker Text"


def make_user(username):
    return User.objects.create_user(username=username, password="pw", is_staff=True)


def make_usecase(*, slug, title, intro="i", body="b", outro="o", persona="",
                 language="en", author=None, status=EditorialWorkflowMixin.STATUS_DRAFT):
    """A use case that was never published: no live snapshot at all."""
    usecase = UseCase.objects.create(author=author, status=status)
    usecase.create_translation(
        language, title=title, intro=intro, body=body, outro=outro,
        slug=slug, persona=persona,
    )
    return usecase


def add_translation(usecase, language, *, slug, title, intro="i", body="b",
                    outro="o", persona=""):
    usecase.create_translation(
        language, title=title, intro=intro, body=body, outro=outro,
        slug=slug, persona=persona,
    )
    return usecase


def publish(usecase, by):
    """
    Run the real FSM publish so ``live_i18n`` is populated for real, then set
    the live-revision marker the admin would set (see module docstring).
    """
    if usecase.status != EditorialWorkflowMixin.STATUS_APPROVED:
        if usecase.status not in (
            EditorialWorkflowMixin.STATUS_REVIEW,
            EditorialWorkflowMixin.STATUS_APPROVED,
        ):
            usecase.move_to_review(by=by)
            usecase.save()
        usecase.approve(by=by)
        usecase.save()
    usecase.publish(by=by)
    if not usecase.published_at:
        usecase.published_at = timezone.now()
    usecase.save()

    fresh = UseCase.objects.get(pk=usecase.pk)
    fresh.last_published_revision_id = 1
    fresh.save(update_fields=["last_published_revision_id"])
    return fresh


def save_draft_edit(usecase, language, **fields):
    """
    Write new values into the *current* translation row only.

    Re-reads the row first, exactly as the admin change form does: publishing
    writes ``live_i18n`` onto a different instance, so saving a stale
    in-memory object here would silently write the snapshot back and destroy
    the very divergence these tests are about. ``live_i18n`` itself is never
    touched.
    """
    fresh = UseCase.objects.get(pk=usecase.pk)
    fresh.set_current_language(language)
    for name, value in fields.items():
        setattr(fresh, name, value)
    fresh.save()
    return fresh


def start_review_round(usecase, by):
    """
    Move a published use case back into review, exactly as the admin's
    auto-review guard (EditorialWorkflowAdminMixin._must_auto_review) does
    when an author saves a change to a published object.
    """
    fresh = UseCase.objects.get(pk=usecase.pk)
    fresh.move_to_review(by=by)
    fresh.save()
    return UseCase.objects.get(pk=fresh.pk)


def archive(usecase, by):
    fresh = UseCase.objects.get(pk=usecase.pk)
    fresh.archive(by=by)
    fresh.is_published = False
    fresh.save()
    return UseCase.objects.get(pk=fresh.pk)
