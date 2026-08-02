from __future__ import annotations

from collections.abc import Iterable

from django.contrib.auth.models import Group, Permission
from django.db.models import Q, QuerySet

EDITORIAL_APPS: tuple[str, ...] = (
    "compare",
    "guides",
    "prompts",
    "usecases",
)

GROUP_DEFINITIONS = (
    (
        "Admin",
        {"apps": None, "prefixes": ()}),
    (
        "Editor",
        {"apps": EDITORIAL_APPS, "prefixes": ("add", "change", "view")},
    ),
    (
        "Author",
        {"apps": EDITORIAL_APPS, "prefixes": ("add", "view")},
    ),
)


def _permissions_for(app_labels: Iterable[str], prefixes: Iterable[str]) -> QuerySet[Permission]:
    query = Q(content_type__app_label__in=app_labels)
    if prefixes:
        prefix_query = Q()
        for prefix in prefixes:
            prefix_query |= Q(codename__startswith=f"{prefix}_")
        query &= prefix_query
    return Permission.objects.filter(query)


def ensure_editorial_groups(sender, **kwargs) -> None:
    for name, config in GROUP_DEFINITIONS:
        group, _ = Group.objects.get_or_create(name=name)
        apps = config.get("apps")
        prefixes = config.get("prefixes", ())
        if apps is None:
            permissions = Permission.objects.all()
        else:
            permissions = _permissions_for(apps, prefixes)
        group.permissions.set(permissions)
