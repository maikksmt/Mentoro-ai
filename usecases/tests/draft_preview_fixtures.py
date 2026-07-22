"""
Shared builders for the Beta 11.8 use-case-draft-preview tests.

Mirrors ``guides/tests/draft_preview_fixtures.py`` (Beta 11.4) and
``prompts/tests/draft_preview_fixtures.py`` (Beta 11.5) in naming and shape,
so the preview test modules read the same way across all three content
types. Reuses ``usecases/tests/live_visibility_fixtures.py`` (Beta 11.7) for
the pieces that already have exactly the right signature - ``publish()``,
``save_translation_edit()``, ``add_translation()``, ``archive()`` - rather
than duplicating them; only ``make_user()`` (group/superuser support, as the
permission tests need) and ``make_draft_usecase()`` (the guide/prompt-style
name for a never-published use case) are new here.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from usecases.tests.live_visibility_fixtures import (  # noqa: F401
    add_translation,
    archive,
    make_usecase,
    publish,
    save_draft_edit as save_translation_edit,
)

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


def make_draft_usecase(author, *, slug, title, intro="i", body="b", outro="o",
                       persona="", language="en"):
    """A use case that was never published: no live snapshot at all."""
    return make_usecase(
        slug=slug, title=title, intro=intro, body=body, outro=outro,
        persona=persona, language=language, author=author,
    )
