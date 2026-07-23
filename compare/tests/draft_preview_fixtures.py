"""
Shared builders for the Beta 11.10 comparison-draft-preview tests.

Mirrors ``guides/tests/draft_preview_fixtures.py`` (Beta 11.4),
``prompts/tests/draft_preview_fixtures.py`` (Beta 11.5) and
``usecases/tests/draft_preview_fixtures.py`` (Beta 11.8) in naming and
shape. Reuses ``compare/tests/live_snapshot_fixtures.py`` (Beta 11.9) for
everything that already has exactly the right signature - ``publish()``,
``save_draft_edit()``, ``add_translation()``, ``add_entry()``,
``add_entry_translation()``, ``start_review_round()``, ``request_rework()``,
``archive()``, ``make_tool()``, ``save_entry_draft_edit()`` - rather than
duplicating them; only ``make_user()`` (group/superuser support, as the
permission tests need) and ``make_draft_comparison()`` (the
guide/prompt/usecase-style name for a never-published comparison) are new
here.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from compare.tests.live_snapshot_fixtures import (  # noqa: F401
    add_entry,
    add_entry_translation,
    add_translation,
    archive,
    make_comparison,
    make_tool,
    publish,
    request_rework,
    save_draft_edit as save_translation_edit,
    save_entry_draft_edit,
    start_review_round,
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


def make_draft_comparison(author, *, slug, title, intro="i", body="b", language="en"):
    """A comparison that was never published: no live snapshot at all."""
    return make_comparison(
        slug=slug, title=title, intro=intro, body=body, language=language, author=author,
    )
