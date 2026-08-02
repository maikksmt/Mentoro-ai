"""
Shared builders for the Beta 11.4 guide-draft-preview tests.

Mirrors the existing ``search/tests/editorial_fixtures.py`` convention: one
small helper module so the six focused preview test modules stay readable
without repeating the same publish-then-edit choreography.

Two ordering details matter and are handled here:

* Sections are created *before* publishing. ``guides/signals.py`` knocks a
  published guide back into ``review`` whenever a section is saved, so
  building the structure first keeps the publish step meaningful.
* After publishing, editing a translation only changes the current
  translation row - ``live_i18n`` keeps the published snapshot. That
  divergence is exactly what the preview has to surface and the public page
  has to keep hiding.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from core.models.editorial import EditorialWorkflowMixin
from guides.models import Guide, GuideItem, GuideSection

User = get_user_model()


def make_user(username, *, group=None, superuser=False, staff=True):
    if superuser:
        return User.objects.create_superuser(
            username=username, email=f"{username}@example.com", password="pw"
        )
    user = User.objects.create_user(username=username, password="pw", is_staff=staff)
    if group:
        user.groups.add(Group.objects.get(name=group))
    return user


def make_draft_guide(author, *, slug, title, intro="i", body="b", language="en"):
    """A guide that was never published: no live snapshot at all."""
    guide = Guide.objects.create(
        author=author, status=EditorialWorkflowMixin.STATUS_DRAFT
    )
    guide.create_translation(language, title=title, intro=intro, body=body, slug=slug)
    return guide


def add_section(guide, *, order, translations):
    """``translations`` maps language code -> (title, body)."""
    section = GuideSection.objects.create(guide=guide, order=order)
    for language, (title, body) in translations.items():
        section.create_translation(language, title=title, body=body)
    return section


def add_item(section, *, order, translations, url="https://example.com/item/", kind="guide"):
    """``translations`` maps language code -> (title, teaser)."""
    item = GuideItem.objects.create(
        section=section, order=order, url=url, kind=kind, is_published=True
    )
    for language, (title, teaser) in translations.items():
        item.create_translation(language, title=title, teaser=teaser)
    return item


def publish(guide, author):
    """Run the real FSM publish so ``live_i18n`` is populated for real."""
    if guide.status != EditorialWorkflowMixin.STATUS_APPROVED:
        if guide.status not in (
            EditorialWorkflowMixin.STATUS_REVIEW,
            EditorialWorkflowMixin.STATUS_APPROVED,
        ):
            guide.move_to_review(by=author)
            guide.save()
        guide.approve(by=author)
        guide.save()
    guide.publish(by=author)
    guide.save()
    return guide


def save_translation_edit(obj, language, **fields):
    """
    Write new values into the *current* translation row only.

    Re-reads the row first, exactly as the admin change form does. That
    matters: publishing writes ``live_i18n`` onto a *different* instance
    (``Guide.on_after_publish()`` re-queries its sections), so saving a stale
    in-memory object here would silently write the snapshot back to ``{}``
    and destroy the very divergence these tests are about.

    ``live_i18n`` itself is never touched, so afterwards the published
    snapshot and the saved draft deliberately differ - the core scenario the
    preview exists for.
    """
    fresh = obj.__class__.objects.get(pk=obj.pk)
    fresh.set_current_language(language)
    for name, value in fields.items():
        setattr(fresh, name, value)
    fresh.save()
    return fresh


def keep_publicly_visible(guide):
    """
    Let a guide that fell back to ``review`` stay on the public site.

    ``EditorialQuerySet.visible_on_site()`` admits review/approved guides that
    already have a live revision, which is what a real mid-revision guide
    looks like.
    """
    guide.last_published_revision_id = 1
    guide.save(update_fields=["last_published_revision_id"])
    return guide
