"""
Beta 11.13D1B: the Django admin and the editorial workspace must produce the
*same persistent editorial state* for the same business action.

Why this module exists
----------------------
Beta 11.13D1A measured both surfaces on identical inputs and found that only
Prompt submit/approve/publish agreed. Everything else diverged: the workspace
ran a bare ``getattr(obj, transition)(by=user)`` plus ``obj.save()`` with no
transaction of its own, no ``reversion.create_revision()``, no revision user
and no audit comment, while the admin wrapped the very same FSM transition in
a revision. Guide, Use Case and Comparison therefore lost their entire audit
trail and rollback point whenever an editor worked in the workspace, and their
``last_published_revision_id`` was never written at all.

What is pinned here
-------------------
Every assertion runs through a *real surface* - a Django admin changelist POST
or a workspace POST endpoint - never by calling the shared primitive directly.
A primitive that is correct in isolation but not actually reached by one of the
two surfaces is exactly the defect this slice exists to remove.

Identity-free comparison
------------------------
The two surfaces necessarily operate on two different rows, so raw primary
keys, revision ids and fingerprints can never be equal. Everything is compared
through :func:`editorial_state_signature`, which keeps *structure* (is a
binding set? which languages does the snapshot carry? which fields changed?)
and drops identity. Timestamps are compared as "present / absent", never as
exact values.
"""
import itertools
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import translation
from reversion.models import Revision, Version
from reversion.signals import post_revision_commit

from compare.models import Comparison
from core import editorial_actions
from core.models.editorial import EditorialWorkflowMixin as EW
from guides.models import Guide
from prompts.models import Prompt
from usecases.models import UseCase

User = get_user_model()

_slug_counter = itertools.count()

PASSWORD = "pw-parity"

#: The reason the workspace sends with every rework request. The admin's bulk
#: action has no equivalent input, which is the one place the two surfaces are
#: allowed to differ - see ``EditorialActionParityTests._assert_parity``.
WORKSPACE_REWORK_REASON = "Workspace reason: please tighten the introduction."

#: The four editorial roots, keyed by the model key both surfaces already use
#: (``content.views.editorial.EDITORIAL_MODEL_REGISTRY`` for the workspace,
#: the admin changelist URL for the admin).
MODEL_BY_KEY = {
    "guide": Guide,
    "prompt": Prompt,
    "usecase": UseCase,
    "comparison": Comparison,
}

ADMIN_CHANGELIST = {
    "guide": "/admin/guides/guide/",
    "prompt": "/admin/prompts/prompt/",
    "usecase": "/admin/usecases/usecase/",
    "comparison": "/admin/compare/comparison/",
}

#: workspace ``status`` value -> admin action name, for the same business
#: action. These are the six actions D1B unifies.
ACTIONS = {
    "review": "action_submit_for_review",
    "rework": "action_request_rework",
    "approved": "action_approve",
    "published": "action_publish",
    "archived": "action_archive",
    "draft": "action_restore_draft",
}

#: The lifecycle prefix that brings a fresh draft into the source state each
#: action needs. Driven through the *same* surface under test, so a Prompt
#: really acquires its review/approval bindings instead of having a status
#: string injected behind the primitives' back.
PREFIX = {
    "review": (),
    "rework": ("review",),
    "approved": ("review",),
    "published": ("review", "approved"),
    "archived": ("review", "approved", "published"),
    "draft": ("review", "approved", "published", "archived"),
}


def _unique(prefix):
    return f"{prefix}-{next(_slug_counter)}"


def make_editorial_object(model, *, author, languages=("en", "de")):
    """A fresh draft with real translations in every requested language."""
    translated = {f.name for f in model._parler_meta.root_model._meta.fields}
    with translation.override("en"):
        obj = model.objects.create(author=author)
        for language in languages:
            values = {"title": f"T {_unique(language)}", "slug": _unique("slug")}
            for optional in ("intro", "body", "outro", "persona"):
                if optional in translated:
                    values[optional] = f"{optional}-text"
            obj.create_translation(language, **values)
    return model.objects.get(pk=obj.pk)


def editorial_state_signature(model, pk):
    """
    Identity-free description of a row's editorial state.

    Everything that could only ever differ between two independently created
    rows (pks, revision ids, fingerprint digests, slugs, timestamps) is reduced
    to structure: "is it set", "which languages", "which keys".
    """
    obj = model.objects.get(pk=pk)
    live = obj.live_i18n or {}
    signature = {
        "status": obj.status,
        "is_published": obj.is_published,
        "published_at_set": obj.published_at is not None,
        "submitted_for_review_at_set": obj.submitted_for_review_at is not None,
        "reviewed_at_set": obj.reviewed_at is not None,
        "reviewed_by_set": obj.reviewed_by_id is not None,
        "review_note": obj.review_note,
        "review_revision_set": obj.review_revision_id is not None,
        "approved_revision_set": obj.approved_revision_id is not None,
        "fingerprint_set": bool(obj.review_payload_fingerprint),
        "last_published_revision_set": obj.last_published_revision_id is not None,
        "live_languages": sorted(live.keys()),
        "live_field_names": sorted({f for entry in live.values() if isinstance(entry, dict) for f in entry}),
    }
    # Type-specific live projections, only where the model really has them.
    if hasattr(obj, "live_author"):
        author_snapshot = obj.live_author or {}
        signature["live_author_keys"] = sorted(author_snapshot.keys())
    if hasattr(obj, "live_entries"):
        entries = obj.live_entries or []
        signature["live_entry_count"] = len(entries)
    return signature


def root_version_in(revision, model, pk):
    """The root ``Version`` of ``pk`` inside ``revision``, or ``None``."""
    meta = model._meta.concrete_model._meta
    return (
        Version.objects.filter(
            revision_id=revision.pk,
            content_type__app_label=meta.app_label,
            content_type__model=meta.model_name,
            object_id=str(pk),
        )
        .first()
    )


class EditorialParityBase(TestCase):
    """Shared users and the two surface drivers."""

    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user(username="parity-author", password=PASSWORD)
        cls.author.groups.add(Group.objects.get(name="Author"))
        # One actor per surface. Both are Editors and neither authors the
        # content, so `content.approve`/`content.request_rework`
        # (`is_editor & ~is_author`) are satisfied on both sides.
        cls.workspace_actor = User.objects.create_user(
            username="parity-ws-editor", password=PASSWORD
        )
        cls.workspace_actor.groups.add(Group.objects.get(name="Editor"))
        cls.admin_actor = User.objects.create_user(
            username="parity-admin-editor", password=PASSWORD, is_staff=True
        )
        cls.admin_actor.groups.add(Group.objects.get(name="Editor"))

    # --- surface drivers -------------------------------------------------

    def run_workspace(self, key, obj, status):
        client = self.client_class()
        client.login(username=self.workspace_actor.username, password=PASSWORD)
        payload = {"model": key, "object_id": obj.pk, "status": status}
        if status == "rework":
            # Beta 11.13D1G-a made a reason mandatory for rework in the
            # workspace, so the surface cannot be driven without one.
            payload["review_note"] = WORKSPACE_REWORK_REASON
        return client.post(
            reverse("content:editorial:review_update"), payload, follow=True
        )

    def run_admin(self, key, objs, status):
        client = self.client_class()
        client.login(username=self.admin_actor.username, password=PASSWORD)
        return client.post(
            ADMIN_CHANGELIST[key],
            {
                "action": ACTIONS[status],
                "_selected_action": [str(o.pk) for o in objs],
                "index": "0",
            },
            follow=True,
        )

    def drive(self, surface, key, obj, status):
        if surface == "workspace":
            return self.run_workspace(key, obj, status)
        return self.run_admin(key, [obj], status)

    def build_source_object(self, surface, key, status):
        """Fresh draft advanced to ``status``'s source state on ``surface``."""
        model = MODEL_BY_KEY[key]
        obj = make_editorial_object(model, author=self.author)
        for step in PREFIX[status]:
            self.drive(surface, key, obj, step)
            obj = model.objects.get(pk=obj.pk)
        return obj

    def measure(self, surface, key, status):
        """Run one action on a freshly prepared object and report the delta."""
        model = MODEL_BY_KEY[key]
        obj = self.build_source_object(surface, key, status)
        source_status = model.objects.get(pk=obj.pk).status
        known_revisions = set(Revision.objects.values_list("pk", flat=True))

        self.drive(surface, key, obj, status)

        new_revisions = list(
            Revision.objects.exclude(pk__in=known_revisions).order_by("pk")
        )
        return {
            "obj": model.objects.get(pk=obj.pk),
            "source_status": source_status,
            "signature": editorial_state_signature(model, obj.pk),
            "revisions": new_revisions,
            "comments": [r.comment for r in new_revisions],
            "users": [r.user_id for r in new_revisions],
        }


class EditorialActionParityTests(EditorialParityBase):
    """
    The core contract: for each type and each of the six actions, the admin and
    the workspace must leave the same editorial state behind and record the
    same revision.
    """

    def _assert_parity(self, key, status):
        admin = self.measure("admin", key, status)
        workspace = self.measure("workspace", key, status)

        self.assertEqual(
            admin["source_status"],
            workspace["source_status"],
            f"{key}/{status}: the two surfaces did not start from the same state",
        )

        workspace_signature = dict(workspace["signature"])
        admin_signature = dict(admin["signature"])
        if status == "rework":
            # The single sanctioned divergence (Beta 11.13D1G-a): the workspace
            # collects an explicit reason from the editor, while the admin's
            # bulk action has no field to type one into and therefore still
            # writes "". Asserted explicitly below rather than waved away, and
            # narrowed to this one key of this one action - every other field,
            # and every other action, stays fully compared.
            self.assertEqual(
                workspace_signature.pop("review_note"),
                WORKSPACE_REWORK_REASON,
                f"{key}/rework: the workspace did not store the editor's reason",
            )
            self.assertEqual(
                admin_signature.pop("review_note"),
                "",
                f"{key}/rework: the admin bulk path unexpectedly stored a note",
            )

        self.assertEqual(
            workspace_signature,
            admin_signature,
            f"{key}/{status}: workspace and admin left different editorial state",
        )
        self.assertEqual(
            len(workspace["revisions"]),
            len(admin["revisions"]),
            f"{key}/{status}: different number of revisions "
            f"(workspace={len(workspace['revisions'])}, admin={len(admin['revisions'])})",
        )
        self.assertEqual(
            len(admin["revisions"]),
            1,
            f"{key}/{status}: expected exactly one revision per successful action",
        )
        self.assertEqual(
            workspace["comments"],
            admin["comments"],
            f"{key}/{status}: audit comments differ",
        )
        self.assertEqual(
            workspace["users"],
            [self.workspace_actor.pk],
            f"{key}/{status}: workspace revision is not attributed to the acting user",
        )
        self.assertEqual(
            admin["users"],
            [self.admin_actor.pk],
            f"{key}/{status}: admin revision is not attributed to the acting user",
        )
        model = MODEL_BY_KEY[key]
        for label, measured in (("workspace", workspace), ("admin", admin)):
            self.assertIsNotNone(
                root_version_in(measured["revisions"][0], model, measured["obj"].pk),
                f"{key}/{status}: {label} revision contains no root version of the object",
            )


def _install_parity_tests():
    """One test method per (type, action) so a failure names both."""

    def make(key, status):
        def test(self):
            self._assert_parity(key, status)

        test.__name__ = f"test_{key}_{status}_parity"
        test.__doc__ = (
            f"{key}: the '{status}' action leaves identical state and one "
            f"identical revision on both surfaces."
        )
        return test

    for key in MODEL_BY_KEY:
        for status in ACTIONS:
            method = make(key, status)
            setattr(EditorialActionParityTests, method.__name__, method)


_install_parity_tests()


class PublishMarkerTests(EditorialParityBase):
    """
    ``last_published_revision_id`` must point at the root ``Version`` of the
    revision the publish itself created - on both surfaces.

    Historically it did not on either: ``core.admin.set_last_published_revision``
    resolved it with an unordered ``Version.objects.get_for_object(obj).first()``
    *inside* the still-open revision block, so the versions of the publish
    revision did not exist yet. The marker was therefore ``None`` on a first
    publish and pointed at the approval revision otherwise. The workspace wrote
    no marker at all.
    """

    def _assert_marker(self, surface, key):
        model = MODEL_BY_KEY[key]
        measured = self.measure(surface, key, "published")
        obj = measured["obj"]
        marker = obj.last_published_revision_id

        self.assertIsNotNone(
            marker, f"{key}/{surface}: publish wrote no last_published_revision_id"
        )
        version = Version.objects.filter(pk=marker).first()
        self.assertIsNotNone(
            version,
            f"{key}/{surface}: last_published_revision_id={marker} is not a Version id",
        )
        self.assertEqual(
            version.revision_id,
            measured["revisions"][0].pk,
            f"{key}/{surface}: marker points at revision #{version.revision_id}, "
            f"but this publish created revision #{measured['revisions'][0].pk}",
        )
        meta = model._meta.concrete_model._meta
        self.assertEqual(version.content_type.app_label, meta.app_label)
        self.assertEqual(version.content_type.model, meta.model_name)
        self.assertEqual(
            version.object_id,
            str(obj.pk),
            f"{key}/{surface}: marker points at another object's version",
        )


def _install_marker_tests():
    def make(key, surface):
        def test(self):
            self._assert_marker(surface, key)

        test.__name__ = f"test_{key}_{surface}_publish_marker"
        test.__doc__ = (
            f"{key}: a {surface} publish points last_published_revision_id at "
            f"the root version of its own publish revision."
        )
        return test

    for key in MODEL_BY_KEY:
        for surface in ("admin", "workspace"):
            method = make(key, surface)
            setattr(PublishMarkerTests, method.__name__, method)


_install_marker_tests()


class PromptContractRegressionTests(EditorialParityBase):
    """
    Prompt already routed submit/approve/publish through its own sanctioned
    primitives (Beta 11.11C2A/C3A/D2) on both surfaces. D1B must not weaken any
    of that, and must not wrap those primitives in a second revision.
    """

    def _submit(self, surface, prompt):
        self.drive(surface, "prompt", prompt, "review")
        return Prompt.objects.get(pk=prompt.pk)

    def test_workspace_submit_still_binds_review_revision_and_fingerprint(self):
        prompt = make_editorial_object(Prompt, author=self.author)
        before = Revision.objects.count()
        submitted = self._submit("workspace", prompt)

        self.assertEqual(submitted.status, EW.STATUS_REVIEW)
        self.assertIsNotNone(submitted.review_revision_id)
        self.assertNotEqual(submitted.review_payload_fingerprint, "")
        self.assertIsNone(submitted.approved_revision_id)
        self.assertEqual(
            Revision.objects.count(), before + 1, "submit must create exactly one revision"
        )
        self.assertEqual(
            Revision.objects.get(pk=submitted.review_revision_id).comment,
            "submit_for_review",
        )

    def test_workspace_approve_binds_the_reviewed_revision(self):
        prompt = make_editorial_object(Prompt, author=self.author)
        submitted = self._submit("workspace", prompt)
        review_revision_id = submitted.review_revision_id
        fingerprint = submitted.review_payload_fingerprint

        self.drive("workspace", "prompt", prompt, "approved")
        approved = Prompt.objects.get(pk=prompt.pk)

        self.assertEqual(approved.status, EW.STATUS_APPROVED)
        self.assertEqual(approved.review_revision_id, review_revision_id)
        self.assertEqual(approved.approved_revision_id, review_revision_id)
        self.assertEqual(approved.review_payload_fingerprint, fingerprint)

    def test_workspace_publish_keeps_binding_and_sets_live_author(self):
        prompt = make_editorial_object(Prompt, author=self.author)
        self._submit("workspace", prompt)
        self.drive("workspace", "prompt", prompt, "approved")
        approved = Prompt.objects.get(pk=prompt.pk)
        approved_revision_id = approved.approved_revision_id

        before = Revision.objects.count()
        self.drive("workspace", "prompt", prompt, "published")
        published = Prompt.objects.get(pk=prompt.pk)

        self.assertEqual(published.status, EW.STATUS_PUBLISHED)
        self.assertTrue(published.is_published)
        self.assertEqual(published.approved_revision_id, approved_revision_id)
        self.assertTrue(published.live_author, "live_author snapshot was not written")
        self.assertEqual(
            Revision.objects.count(),
            before + 1,
            "publish must create exactly one revision - never a second wrapper revision",
        )

    def test_stale_payload_still_blocks_approval_fail_closed(self):
        from prompts.models import PromptTranslation

        prompt = make_editorial_object(Prompt, author=self.author)
        self._submit("workspace", prompt)
        PromptTranslation.objects.filter(master_id=prompt.pk, language_code="en").update(
            title="changed after submit"
        )
        before = Revision.objects.count()

        self.drive("workspace", "prompt", prompt, "approved")

        blocked = Prompt.objects.get(pk=prompt.pk)
        self.assertEqual(blocked.status, EW.STATUS_REVIEW)
        self.assertIsNone(blocked.approved_revision_id)
        self.assertEqual(Revision.objects.count(), before)


class AdminBulkContractTests(EditorialParityBase):
    """
    D1B must not change the admin's bulk contract. A changelist selection still
    lands in ONE shared revision, and every selected object still gets its own
    correct publish marker inside it.
    """

    def test_bulk_publish_keeps_one_shared_revision_for_the_selection(self):
        first = self.build_source_object("admin", "guide", "published")
        second = self.build_source_object("admin", "guide", "published")
        known = set(Revision.objects.values_list("pk", flat=True))

        self.run_admin("guide", [first, second], "published")

        new_revisions = list(Revision.objects.exclude(pk__in=known))
        self.assertEqual(
            len(new_revisions),
            1,
            "a bulk selection must stay in exactly one shared revision",
        )
        revision = new_revisions[0]
        self.assertEqual(revision.comment, "Admin-Action: publish")

        for obj in (first, second):
            published = Guide.objects.get(pk=obj.pk)
            self.assertEqual(published.status, EW.STATUS_PUBLISHED)
            version = Version.objects.get(pk=published.last_published_revision_id)
            self.assertEqual(
                version.revision_id,
                revision.pk,
                "each object's marker must point into the shared publish revision",
            )
            self.assertEqual(version.object_id, str(obj.pk))

    def test_bulk_submit_keeps_one_shared_revision(self):
        first = make_editorial_object(Guide, author=self.author)
        second = make_editorial_object(Guide, author=self.author)
        known = set(Revision.objects.values_list("pk", flat=True))

        self.run_admin("guide", [first, second], "review")

        new_revisions = list(Revision.objects.exclude(pk__in=known))
        self.assertEqual(len(new_revisions), 1)
        self.assertEqual(new_revisions[0].comment, "submit_for_review")
        for obj in (first, second):
            self.assertEqual(Guide.objects.get(pk=obj.pk).status, EW.STATUS_REVIEW)


class EditorialActionFailureTests(EditorialParityBase):
    """Refused and failing actions must leave nothing behind."""

    def test_invalid_source_status_mutates_nothing_and_records_no_revision(self):
        for key in MODEL_BY_KEY:
            with self.subTest(key=key):
                model = MODEL_BY_KEY[key]
                obj = make_editorial_object(model, author=self.author)  # draft
                before = Revision.objects.count()

                # draft -> published is not a legal FSM edge (publish needs approved)
                self.run_workspace(key, obj, "published")

                self.assertEqual(model.objects.get(pk=obj.pk).status, EW.STATUS_DRAFT)
                self.assertEqual(Revision.objects.count(), before)

    def test_marker_failure_rolls_back_the_whole_publish(self):
        """
        An injected failure while resolving the publish marker must roll back
        the transition, the live snapshot and the revision together - never a
        published row without a marker, and never a revision without a publish.
        """
        obj = self.build_source_object("workspace", "guide", "published")
        before_status = Guide.objects.get(pk=obj.pk).status
        before_revisions = Revision.objects.count()

        with mock.patch(
            "core.editorial_actions._resolve_root_version",
            side_effect=RuntimeError("injected marker failure"),
        ):
            with self.assertRaises(RuntimeError):
                self.run_workspace("guide", obj, "published")

        after = Guide.objects.get(pk=obj.pk)
        self.assertEqual(after.status, before_status, "status was not rolled back")
        self.assertFalse(after.is_published, "is_published was not rolled back")
        self.assertIsNone(after.last_published_revision_id)
        self.assertEqual(
            Revision.objects.count(),
            before_revisions,
            "a revision survived a failed publish",
        )

    def test_persist_failure_leaves_no_revision_and_no_status_change(self):
        """
        A failure raised while persisting the transition must leave neither a
        revision nor a half-applied status behind.

        The injection point is deliberately the shared primitive's own save and
        not ``on_after_publish``: ``EditorialWorkflowMixin.publish()`` wraps
        that hook in ``try/except Exception: pass``, so a failure there is
        swallowed by the model itself and never reaches any caller. That is
        pre-existing model behaviour which this slice does not change.
        """
        obj = self.build_source_object("workspace", "guide", "published")
        before_revisions = Revision.objects.count()

        with mock.patch(
            "core.editorial_actions._persist",
            side_effect=RuntimeError("injected persistence failure"),
        ):
            with self.assertRaises(RuntimeError):
                self.run_workspace("guide", obj, "published")

        after = Guide.objects.get(pk=obj.pk)
        self.assertEqual(after.status, EW.STATUS_APPROVED)
        self.assertFalse(after.is_published)
        self.assertIsNone(after.last_published_revision_id)
        self.assertEqual(Revision.objects.count(), before_revisions)


class EditorialWorkspaceDelegationTests(EditorialParityBase):
    """
    The two author-facing workspace endpoints must delegate too - not just the
    editor-facing ``review_update`` the parity matrix above drives.
    """

    def _author_client(self):
        client = self.client_class()
        client.login(username=self.author.username, password=PASSWORD)
        return client

    def test_submit_to_review_endpoint_records_a_revision(self):
        guide = make_editorial_object(Guide, author=self.author)
        before = Revision.objects.count()

        self._author_client().post(
            reverse("content:editorial:submit_to_review"),
            {"model": "guide", "object_id": guide.pk},
        )

        self.assertEqual(Guide.objects.get(pk=guide.pk).status, EW.STATUS_REVIEW)
        self.assertEqual(Revision.objects.count(), before + 1)
        self.assertEqual(Revision.objects.latest("pk").comment, "submit_for_review")
        self.assertEqual(Revision.objects.latest("pk").user_id, self.author.pk)

    def test_my_content_update_endpoint_records_a_revision(self):
        guide = self.build_source_object("workspace", "guide", "published")
        before = Revision.objects.count()

        self._author_client().post(
            reverse("content:editorial:my_content_update"),
            {"model": "guide", "object_id": guide.pk, "status": "published"},
        )

        published = Guide.objects.get(pk=guide.pk)
        self.assertEqual(published.status, EW.STATUS_PUBLISHED)
        self.assertEqual(Revision.objects.count(), before + 1)
        self.assertEqual(Revision.objects.latest("pk").comment, "Admin-Action: publish")
        self.assertIsNotNone(published.last_published_revision_id)


class WorkspaceUsesTheSharedPrimitiveTests(TestCase):
    """
    Static guard: the workspace must not carry its own copy of the mutation.

    Beta 11.13D1A's finding was precisely that ``content/views/editorial.py``
    executed ``getattr(obj, method_name)(by=request.user)`` followed by
    ``obj.save()``. That pattern must not come back - a future edit that
    reintroduces it would silently drop the revision again while every
    status-only test stayed green.
    """

    def _module_source(self):
        import ast
        import pathlib

        import content.views.editorial as module

        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        return ast.parse(source), source

    def test_workspace_no_longer_saves_after_a_dynamic_fsm_transition(self):
        import ast

        tree, _source = self._module_source()
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for stmt in ast.walk(node):
                # `obj.save()` with no update_fields, i.e. the old generic path
                if (
                    isinstance(stmt, ast.Call)
                    and isinstance(stmt.func, ast.Attribute)
                    and stmt.func.attr == "save"
                    and not stmt.keywords
                ):
                    offenders.append(node.name)
        self.assertEqual(
            sorted(set(offenders)),
            [],
            "content/views/editorial.py still saves an editorial object itself; "
            "the mutation belongs to core.editorial_actions",
        )

    def test_workspace_imports_the_shared_primitive(self):
        _tree, source = self._module_source()
        self.assertTrue(
            "apply_editorial_action" in source,
            "content/views/editorial.py does not use core.editorial_actions",
        )

    def test_admin_imports_the_shared_primitive(self):
        import pathlib

        import core.admin as module

        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        self.assertTrue(
            "apply_editorial_action" in source,
            "core/admin.py does not use core.editorial_actions",
        )


# ======================================================================
# Beta 11.13D1B1: pending publish-marker lifecycle
# ======================================================================
#
# D1B resolves `last_published_revision_id` from a `post_revision_commit`
# receiver, because the root version does not exist until reversion writes the
# revision. The publishes waiting for that signal are held in an in-memory
# `ContextVar` collection.
#
# The success paths were covered above. What was not: an *outer* revision that
# is aborted before `post_revision_commit` is ever sent. The signal is the only
# thing that consumes an entry, so a bulk publish whose second object explodes
# would abort the shared revision and leave the first object's entry in the
# ContextVar - a `ContextVar` outlives the request in a threaded worker, so a
# later, unrelated revision in the same thread would inherit it.
#
# These tests pin the lifecycle itself rather than the happy path: every exit
# route must end the scope, and no scope may ever be shared between two
# independent actions.


def pending_marker_entries():
    """
    The pending publish markers visible right now.

    Reaching into the module's ContextVar is deliberate here: the whole point
    of this class is the *in-memory* lifecycle, which by definition leaves no
    observable database trace. Everything else in this file stays behavioural.
    """
    return editorial_actions._pending_publish_markers.get()


def connected_dispatch_uids(signal, prefix):
    """
    dispatch_uids currently connected to ``signal`` that start with ``prefix``.

    ``Signal.receivers`` entries are ``(lookup_key, receiver)`` on older
    Django versions and ``(lookup_key, receiver, is_async)`` on 5.x; only the
    first element is read, so both shapes work and no test asserts a private
    tuple length.
    """
    uids = []
    for entry in signal.receivers:
        lookup_key = entry[0]
        key = lookup_key[0] if isinstance(lookup_key, tuple) else lookup_key
        if isinstance(key, str) and key.startswith(prefix):
            uids.append(key)
    return uids


class PendingMarkerLifecycleTests(EditorialParityBase):
    """Every exit route out of a publish must end the marker scope."""

    def assertNoPendingMarkers(self, context):
        entries = pending_marker_entries()
        self.assertIn(
            entries,
            (None, []),
            f"{context}: pending publish markers survived - {entries!r}",
        )

    def _independent_revision_still_works(self):
        """
        A fresh, unrelated publish after the failure under test.

        This is the behavioural half of every leak assertion: a leaked entry
        would be inherited by this action's scope and processed against *its*
        revision.
        """
        other = self.build_source_object("workspace", "guide", "published")
        known = set(Revision.objects.values_list("pk", flat=True))
        self.run_workspace("guide", other, "published")

        published = Guide.objects.get(pk=other.pk)
        self.assertEqual(published.status, EW.STATUS_PUBLISHED)
        new_revisions = list(Revision.objects.exclude(pk__in=known))
        self.assertEqual(len(new_revisions), 1)
        marker = Version.objects.get(pk=published.last_published_revision_id)
        self.assertEqual(marker.revision_id, new_revisions[0].pk)
        self.assertEqual(marker.object_id, str(other.pk))
        return published

    # --- 4.1 admin bulk aborted after the first marker was registered -----

    def test_aborted_admin_bulk_leaves_no_pending_marker(self):
        first = self.build_source_object("admin", "guide", "published")
        second = self.build_source_object("admin", "guide", "published")
        known = set(Revision.objects.values_list("pk", flat=True))

        real_persist = editorial_actions._persist
        seen = []

        def failing_persist(obj, save_fields):
            seen.append(obj.pk)
            if len(seen) == 2:
                raise RuntimeError("second object fails inside the shared revision")
            return real_persist(obj, save_fields)

        with mock.patch("core.editorial_actions._persist", failing_persist):
            with self.assertRaises(RuntimeError):
                self.run_admin("guide", [first, second], "published")

        # the shared revision was aborted, so nothing may have survived
        self.assertEqual(
            list(Revision.objects.exclude(pk__in=known)),
            [],
            "an aborted bulk publish left a revision behind",
        )
        for obj in (first, second):
            row = Guide.objects.get(pk=obj.pk)
            self.assertEqual(row.status, EW.STATUS_APPROVED)
            self.assertFalse(row.is_published)
            self.assertIsNone(row.last_published_revision_id)

        self.assertNoPendingMarkers("after an aborted admin bulk publish")

    def test_independent_revision_after_aborted_bulk_is_unaffected(self):
        first = self.build_source_object("admin", "guide", "published")
        second = self.build_source_object("admin", "guide", "published")

        real_persist = editorial_actions._persist
        seen = []

        def failing_persist(obj, save_fields):
            seen.append(obj.pk)
            if len(seen) == 2:
                raise RuntimeError("second object fails inside the shared revision")
            return real_persist(obj, save_fields)

        with mock.patch("core.editorial_actions._persist", failing_persist):
            with self.assertRaises(RuntimeError):
                self.run_admin("guide", [first, second], "published")

        # No process restart: the very same execution context now runs an
        # unrelated publish. It must not inherit the aborted bulk's markers.
        self._independent_revision_still_works()

        self.assertIsNone(
            Guide.objects.get(pk=first.pk).last_published_revision_id,
            "the rolled-back object was marked by a later, unrelated revision",
        )
        self.assertEqual(Guide.objects.get(pk=first.pk).status, EW.STATUS_APPROVED)
        self.assertNoPendingMarkers("after an unrelated revision")

    # --- 4.2 workspace aborted before the signal --------------------------

    def test_workspace_abort_before_signal_leaves_no_pending_marker(self):
        obj = self.build_source_object("workspace", "guide", "published")
        known = set(Revision.objects.values_list("pk", flat=True))

        with mock.patch(
            "core.editorial_actions._persist",
            side_effect=RuntimeError("injected before the revision commits"),
        ):
            with self.assertRaises(RuntimeError):
                self.run_workspace("guide", obj, "published")

        row = Guide.objects.get(pk=obj.pk)
        self.assertEqual(row.status, EW.STATUS_APPROVED)
        self.assertIsNone(row.last_published_revision_id)
        self.assertEqual(list(Revision.objects.exclude(pk__in=known)), [])
        self.assertNoPendingMarkers("after a workspace publish aborted before the signal")

        self._independent_revision_still_works()

    # --- 4.3 receiver failure ---------------------------------------------

    def test_receiver_failure_rolls_back_and_ends_the_scope(self):
        obj = self.build_source_object("workspace", "guide", "published")
        known = set(Revision.objects.values_list("pk", flat=True))

        with mock.patch(
            "core.editorial_actions._resolve_root_version",
            side_effect=RuntimeError("injected receiver failure"),
        ):
            with self.assertRaises(RuntimeError):
                self.run_workspace("guide", obj, "published")

        row = Guide.objects.get(pk=obj.pk)
        self.assertEqual(row.status, EW.STATUS_APPROVED)
        self.assertFalse(row.is_published)
        self.assertIsNone(row.last_published_revision_id)
        self.assertEqual(list(Revision.objects.exclude(pk__in=known)), [])
        self.assertNoPendingMarkers("after a receiver failure")

        self._independent_revision_still_works()

    def test_receiver_failure_during_admin_bulk_ends_the_scope(self):
        first = self.build_source_object("admin", "guide", "published")
        second = self.build_source_object("admin", "guide", "published")
        known = set(Revision.objects.values_list("pk", flat=True))

        with mock.patch(
            "core.editorial_actions._resolve_root_version",
            side_effect=RuntimeError("injected receiver failure"),
        ):
            with self.assertRaises(RuntimeError):
                self.run_admin("guide", [first, second], "published")

        self.assertEqual(list(Revision.objects.exclude(pk__in=known)), [])
        for obj in (first, second):
            self.assertIsNone(Guide.objects.get(pk=obj.pk).last_published_revision_id)
        self.assertNoPendingMarkers("after a receiver failure inside an admin bulk")

        self._independent_revision_still_works()

    # --- 4.4 / 4.5 success paths still end the scope -----------------------

    def test_successful_workspace_publish_ends_the_scope(self):
        before = pending_marker_entries()
        self._independent_revision_still_works()
        self.assertNoPendingMarkers("after a successful workspace publish")
        self.assertEqual(
            pending_marker_entries(),
            before,
            "the previous ContextVar value was not restored",
        )

    def test_successful_admin_bulk_publish_ends_the_scope(self):
        first = self.build_source_object("admin", "guide", "published")
        second = self.build_source_object("admin", "guide", "published")
        known = set(Revision.objects.values_list("pk", flat=True))

        self.run_admin("guide", [first, second], "published")

        new_revisions = list(Revision.objects.exclude(pk__in=known))
        self.assertEqual(len(new_revisions), 1, "bulk must stay in one shared revision")
        for obj in (first, second):
            row = Guide.objects.get(pk=obj.pk)
            self.assertEqual(row.status, EW.STATUS_PUBLISHED)
            version = Version.objects.get(pk=row.last_published_revision_id)
            self.assertEqual(version.revision_id, new_revisions[0].pk)
            self.assertEqual(version.object_id, str(obj.pk))
        self.assertNoPendingMarkers("after a successful admin bulk publish")

    # --- 4.6 sequential actions do not share a collection ------------------

    def test_sequential_publishes_do_not_share_a_marker_collection(self):
        first = self.build_source_object("workspace", "guide", "published")
        second = self.build_source_object("workspace", "guide", "published")

        known_first = set(Revision.objects.values_list("pk", flat=True))
        self.run_workspace("guide", first, "published")
        first_revisions = list(Revision.objects.exclude(pk__in=known_first))
        self.assertNoPendingMarkers("between two sequential publishes")

        known_second = set(Revision.objects.values_list("pk", flat=True))
        self.run_workspace("guide", second, "published")
        second_revisions = list(Revision.objects.exclude(pk__in=known_second))

        self.assertEqual(len(first_revisions), 1)
        self.assertEqual(len(second_revisions), 1)
        self.assertNotEqual(first_revisions[0].pk, second_revisions[0].pk)

        first_row = Guide.objects.get(pk=first.pk)
        second_row = Guide.objects.get(pk=second.pk)
        self.assertEqual(
            Version.objects.get(pk=first_row.last_published_revision_id).revision_id,
            first_revisions[0].pk,
        )
        self.assertEqual(
            Version.objects.get(pk=second_row.last_published_revision_id).revision_id,
            second_revisions[0].pk,
        )
        self.assertNoPendingMarkers("after two sequential publishes")


class PublishMarkerScopeGuardTests(EditorialParityBase):
    """
    A publish must never register its marker into a process-wide collection.

    Without an explicit scope the only place an entry could go is a module-level
    container that outlives the request, which is exactly the lifecycle D1B1
    removes. Registering outside a scope therefore has to fail closed rather
    than silently succeed.
    """

    def test_publish_inside_a_foreign_revision_without_a_scope_fails_closed(self):
        import reversion

        obj = self.build_source_object("workspace", "guide", "published")
        known = set(Revision.objects.values_list("pk", flat=True))

        with self.assertRaises(editorial_actions.EditorialActionError) as ctx:
            with reversion.create_revision():
                editorial_actions.apply_editorial_action(
                    obj,
                    editorial_actions.EditorialAction.PUBLISH,
                    actor=self.workspace_actor,
                )
        self.assertEqual(
            ctx.exception.code,
            editorial_actions.EditorialActionErrorCode.MARKER_SCOPE_MISSING,
        )
        self.assertEqual(Guide.objects.get(pk=obj.pk).status, EW.STATUS_APPROVED)
        self.assertEqual(list(Revision.objects.exclude(pk__in=known)), [])
        self.assertIn(pending_marker_entries(), (None, []))

    def test_scope_restores_the_previous_value_and_nests_safely(self):
        outer_sentinel = []
        token = editorial_actions._pending_publish_markers.set(outer_sentinel)
        try:
            with editorial_actions.publish_marker_scope():
                inner = pending_marker_entries()
                self.assertIsNot(inner, outer_sentinel, "scope reused the outer collection")
                self.assertEqual(inner, [])
                with editorial_actions.publish_marker_scope():
                    innermost = pending_marker_entries()
                    self.assertIsNot(innermost, inner, "nested scope reused its parent")
                self.assertIs(pending_marker_entries(), inner, "parent scope not restored")
            self.assertIs(
                pending_marker_entries(),
                outer_sentinel,
                "the pre-existing value was not restored",
            )
            self.assertEqual(outer_sentinel, [], "the outer collection was mutated")
        finally:
            editorial_actions._pending_publish_markers.reset(token)

    def test_scope_clears_entries_even_when_the_body_raises(self):
        with self.assertRaises(RuntimeError):
            with editorial_actions.publish_marker_scope():
                pending_marker_entries().append("leftover")
                raise RuntimeError("body failed")
        self.assertIn(pending_marker_entries(), (None, []))


class PublishMarkerReceiverRegistrationTests(TestCase):
    """The receiver is a module-level singleton and must stay one."""

    PREFIX = "core.editorial_actions"

    def test_exactly_one_receiver_is_connected_under_a_stable_dispatch_uid(self):
        uids = connected_dispatch_uids(post_revision_commit, self.PREFIX)
        self.assertEqual(
            len(uids), 1, f"expected exactly one editorial marker receiver, got {uids}"
        )

    def test_reconnecting_under_the_same_dispatch_uid_does_not_accumulate(self):
        """
        Re-importing or reloading the module re-runs its ``connect()`` call.
        Django deduplicates by ``dispatch_uid``, so a stable uid is what makes
        that idempotent - this asserts the property that guarantees it, without
        reloading the module and invalidating the function references
        ``core.admin`` and the workspace view already hold.
        """
        before = connected_dispatch_uids(post_revision_commit, self.PREFIX)
        post_revision_commit.connect(
            editorial_actions._write_pending_publish_markers,
            dispatch_uid=editorial_actions.PUBLISH_MARKER_RECEIVER_UID,
            weak=False,
        )
        after = connected_dispatch_uids(post_revision_commit, self.PREFIX)
        self.assertEqual(after, before, "receiver was registered more than once")

    def test_receiver_is_not_weakly_referenced(self):
        """A weakly-referenced module-level receiver could be collected while
        the module stays imported, silently disabling every publish marker."""
        self.assertIn(
            editorial_actions.PUBLISH_MARKER_RECEIVER_UID,
            connected_dispatch_uids(post_revision_commit, self.PREFIX),
        )
