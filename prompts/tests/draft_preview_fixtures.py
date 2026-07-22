"""
Shared builders for the Beta 11.5 prompt-draft-preview tests.

Mirrors ``guides/tests/draft_preview_fixtures.py`` (Beta 11.4), scoped to
Prompt's simpler shape (no sections, no items).

After publishing, editing a translation only changes the current translation
row - ``live_i18n`` keeps the published snapshot. That divergence is exactly
what the preview has to surface and the public page has to keep hiding.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from core.models.editorial import EditorialWorkflowMixin
from prompts.models import Prompt

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


def make_draft_prompt(
    author, *, slug, title, intro="i", body="b", outro="o", language="en"
):
    """A prompt that was never published: no live snapshot at all."""
    prompt = Prompt.objects.create(
        author=author, status=EditorialWorkflowMixin.STATUS_DRAFT
    )
    prompt.create_translation(
        language, title=title, intro=intro, body=body, outro=outro, slug=slug
    )
    return prompt


def publish(prompt, author):
    """Run the real FSM publish so ``live_i18n`` is populated for real."""
    if prompt.status != EditorialWorkflowMixin.STATUS_APPROVED:
        if prompt.status not in (
            EditorialWorkflowMixin.STATUS_REVIEW,
            EditorialWorkflowMixin.STATUS_APPROVED,
        ):
            prompt.move_to_review(by=author)
            prompt.save()
        prompt.approve(by=author)
        prompt.save()
    prompt.publish(by=author)
    prompt.save()
    return prompt


def save_translation_edit(obj, language, **fields):
    """
    Write new values into the *current* translation row only.

    Re-reads the row first, exactly as the admin change form does - saving a
    stale in-memory object would silently overwrite fields the fresh publish
    already wrote (e.g. ``live_i18n``) with whatever the earlier instance
    still held. ``live_i18n`` itself is never touched here, so afterwards the
    published snapshot and the saved draft deliberately differ - the core
    scenario the preview exists for.
    """
    fresh = obj.__class__.objects.get(pk=obj.pk)
    fresh.set_current_language(language)
    for name, value in fields.items():
        setattr(fresh, name, value)
    fresh.save()
    return fresh
