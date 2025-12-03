import rules
from django.conf import settings


@rules.predicate
def is_author(user, obj):
    return user.is_authenticated and getattr(obj, "author_id", None) == user.id


@rules.predicate
def is_editor(user, obj):
    return user.is_authenticated and (
            user.is_superuser or user.groups.filter(name__in=getattr(settings, "EDITOR_GROUP_NAMES", ["Editor", "Admin"])).exists())


"""
(Module-level rules registration)
Adds granular permissions for the editorial workflow 
(submit for review, request rework, approve, publish, archive/restore) 
combining author and editor predicates to prevent self-approval by authors.
"""
# Author/Editor: Draft/Rework/Published/Approved → Review
rules.add_perm("content.submit_for_review", is_author | is_editor)

# Editor/Admin: Review → Rework
rules.add_perm("content.request_rework", is_editor & ~is_author)

# Editor/Admin: Review → Approved (Author not allowed to self approve)
rules.add_perm("content.approve", is_editor & ~is_author)

# Author/Editor: Approved → Published
rules.add_perm("content.publish", is_author | is_editor)

# Soft-Delete (Archive)
rules.add_perm("content.archive", is_author | is_editor)

# Restore (from Archive)
rules.add_perm("content.restore", is_author | is_editor)
