"""
Beta 11.11C4D: the frozen, one-time data migration that bridges existing
``prompt-review-v1``-bound ``review``/``approved`` Prompt rows onto the new
``prompt-review-v2`` contract (see ``prompts/review_payload.py`` and
``prompts/migrations/0008_prompt_review_payload_v2.py``).

These tests drive the real migration through Django's ``MigrationExecutor``
against PostgreSQL - never by importing and calling the migration's forward
function as a stand-in for "the migration", except in the one place that
inherently requires a second, independent invocation: proving idempotency
(``IdempotencyTests`` below), which mirrors what a second real
``manage.py migrate`` would do once the migration is already recorded as
applied.

Because ``0008`` is deliberately irreversible (no ``reverse_code`` at all -
see ``NoReverseOperationTests``), rows cannot be seeded by first rolling the
``prompts`` app back to ``0007`` through the executor's normal reverse path.
Instead, every row is seeded through the *real*, currently-installed models
and the real ``submit_prompt_for_review``/``approve_prompt_review``
primitives (0008 makes no schema change, so the schema is identical whether
0007 or 0008 is the app's recorded head) - then the migration's own recorded
"applied" bookkeeping row is deleted directly via ``MigrationRecorder``
(exactly the technique Django's own migration tooling uses for
``--fake``/squash workflows: it only edits the ``django_migrations`` table,
it never touches data or invokes ``reverse_code``) so the real executor runs
the forward ``RunPython`` again, for real, over the freshly seeded rows.
"""
import importlib

from django.contrib.auth import get_user_model
from django.db import DEFAULT_DB_ALIAS, connection, connections
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.test import TransactionTestCase
from django.utils import timezone
from reversion.models import Revision, Version
from taggit.models import Tag as TaggitTag
from taggit.models import TaggedItem

from catalog.models import Tool
from core.models.editorial import EditorialWorkflowMixin as Workflow
from core.review_binding import fingerprint_review_payload
from prompts.models import Prompt, PromptTranslation
from prompts.review_approval import approve_prompt_review
from prompts.review_payload import build_prompt_review_payload
from prompts.review_submission import submit_prompt_for_review

User = get_user_model()

_migration = importlib.import_module("prompts.migrations.0008_prompt_review_payload_v2")

MIGRATION_APP = "prompts"
MIGRATION_NAME = "0008_prompt_review_payload_v2"

_slug_counter_state = {"n": 0}


def _next_slug(prefix):
    _slug_counter_state["n"] += 1
    return f"{prefix}-{_slug_counter_state['n']}"


# ----------------------------------------------------------------------
# seeding helpers - all against the real, currently-installed models
# ----------------------------------------------------------------------


def refetch(pk):
    return Prompt.objects.get(pk=pk)


def make_tool(name):
    tool = Tool.objects.create(slug=_next_slug("mig-tool"))
    tool.create_translation("en", name=name)
    return tool


def make_prompt(*, author=None, languages=("en",), tools=(), tags=(), **extra):
    prompt = Prompt.objects.create(author=author, **extra)
    for language_code in languages:
        prompt.create_translation(
            language_code,
            title=f"Title {language_code}",
            intro="intro",
            body="body",
            outro="outro",
            slug=_next_slug("mig-slug"),
        )
    if tools:
        prompt.tools.add(*tools)
    if tags:
        prompt.tags.add(*tags)
    return prompt


def submitted(*, actor, author=None, languages=("en",), tools=(), tags=()):
    """A prompt carrying a genuine, currently-valid (v2-shaped, since the
    runtime builder is already on v2) review binding - the realistic
    precondition every non-corrupt seed row starts from."""
    prompt = make_prompt(author=author, languages=languages, tools=tools, tags=tags)
    submit_prompt_for_review(prompt, actor=actor)
    return refetch(prompt.pk)


def approved(*, actor, author=None, languages=("en",), tools=(), tags=()):
    prompt = submitted(actor=actor, author=author, languages=languages, tools=tools, tags=tags)
    approve_prompt_review(refetch(prompt.pk), actor=actor)
    return refetch(prompt.pk)


def frozen_v1_fingerprint(pk, alias=DEFAULT_DB_ALIAS):
    """The exact fingerprint the pre-C4D runtime would have stored for this
    row's *current* content - computed through the migration's own frozen,
    historical v1 serializer (imported here only because this is a test, not
    the migration itself)."""
    locked = Prompt.objects.using(alias).select_related("author").get(pk=pk)
    payload = _migration._build_v1_payload(locked, PromptTranslation, TaggedItem, TaggitTag, alias)
    return _migration._fingerprint(payload)


def backdate_to_v1(pk, alias=DEFAULT_DB_ALIAS):
    """Simulates 'this binding was created before C4D': overwrites only the
    fingerprint column with what v1 would have produced for the row's current
    content, exactly as a real v1-era submit would have left it."""
    fingerprint = frozen_v1_fingerprint(pk, alias)
    Prompt.objects.using(alias).filter(pk=pk).update(review_payload_fingerprint=fingerprint)
    return fingerprint


def snapshot(pk, alias=DEFAULT_DB_ALIAS):
    row = Prompt.objects.using(alias).get(pk=pk)
    return {
        "status": row.status,
        "review_revision_id": row.review_revision_id,
        "approved_revision_id": row.approved_revision_id,
        "review_payload_fingerprint": row.review_payload_fingerprint,
        "reviewed_by_id": row.reviewed_by_id,
        "reviewed_at": row.reviewed_at,
        "submitted_for_review_at": row.submitted_for_review_at,
        "review_note": row.review_note,
        "updated_at": row.updated_at,
        "created_at": row.created_at,
        "author_id": row.author_id,
        "live_i18n": row.live_i18n,
        "last_published_revision_id": row.last_published_revision_id,
        "is_published": row.is_published,
        "published_at": row.published_at,
        "translations": sorted(
            row.translations.using(alias).values_list("language_code", "title", "slug")
        ),
        "tags": sorted(row.tags.names()),
        "tools": sorted(row.tools.using(alias).values_list("pk", flat=True)),
    }


def unrelated_revision(alias=DEFAULT_DB_ALIAS):
    """A real, saved Revision that contains no version of anything - stands
    in for 'a revision id that does not actually contain this prompt'."""
    return Revision.objects.using(alias).create(date_created=timezone.now(), comment="unrelated")


class _StubSchemaEditor:
    """The only thing the migration's forward function reads off
    ``schema_editor`` is ``.connection`` - so a second, direct invocation (for
    the idempotency check) only needs to supply that much."""

    def __init__(self, alias):
        self.connection = connections[alias]

    def __getattr__(self, name):  # pragma: no cover - defensive only
        raise AttributeError(name)


def run_migration_via_executor():
    """Deletes 0008's own 'applied' bookkeeping row (not a reverse migration -
    no ``reverse_code`` is ever invoked, see the module docstring) and then
    lets the real executor apply it forward again, for real, against whatever
    rows exist right now."""
    MigrationRecorder(connection).record_unapplied(MIGRATION_APP, MIGRATION_NAME)
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([(MIGRATION_APP, MIGRATION_NAME)])


def run_migration_directly():
    """A second, independent invocation of the same forward function used for
    the idempotency check - exactly what a second ``manage.py migrate`` would
    execute once the migration is already recorded as applied."""
    from django.apps import apps as global_apps

    _migration.migrate_prompt_review_payload_to_v2(global_apps, _StubSchemaEditor(DEFAULT_DB_ALIAS))


class MigrationTestCase(TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.actor = User.objects.create_user(
            f"mig-actor-{_next_slug('u')}", password="pw", first_name="Grace", last_name="Hopper"
        )


# ======================================================================
# Architecture: frozen serializer parity, no forbidden internals, no reverse
# ======================================================================


class FrozenSerializerParityTests(MigrationTestCase):
    """The migration's own frozen v2 builder must produce byte-identical
    output to the real runtime builder for the same row - otherwise the
    'provably migrated' (v2) classification could diverge from what the
    runtime actually considers current."""

    def test_frozen_v2_payload_matches_the_runtime_builder(self):
        tool = make_tool("Parity Tool")
        author = User.objects.create_user("mig-parity-author", password="pw")
        prompt = make_prompt(
            author=author,
            languages=("en", "de"),
            tools=(tool,),
            tags=("parity-alpha", "parity-beta"),
        )
        runtime_payload = build_prompt_review_payload(refetch(prompt.pk))

        locked = Prompt.objects.select_related("author").get(pk=prompt.pk)
        frozen_payload = _migration._build_v2_payload(
            locked, PromptTranslation, TaggedItem, TaggitTag, DEFAULT_DB_ALIAS
        )

        self.assertEqual(runtime_payload, frozen_payload)
        self.assertEqual(
            fingerprint_review_payload(runtime_payload), _migration._fingerprint(frozen_payload)
        )

    def test_frozen_v2_payload_matches_the_runtime_builder_with_no_author(self):
        prompt = make_prompt(author=None)
        runtime_payload = build_prompt_review_payload(refetch(prompt.pk))
        locked = Prompt.objects.select_related("author").get(pk=prompt.pk)
        frozen_payload = _migration._build_v2_payload(
            locked, PromptTranslation, TaggedItem, TaggitTag, DEFAULT_DB_ALIAS
        )
        self.assertEqual(runtime_payload, frozen_payload)

    def test_frozen_v1_payload_uses_the_pre_c4d_author_shape(self):
        author = User.objects.create_user(
            "mig-v1-shape-author", password="pw", first_name="Ada", last_name="Lovelace"
        )
        prompt = make_prompt(author=author)
        locked = Prompt.objects.select_related("author").get(pk=prompt.pk)
        payload = _migration._build_v1_payload(
            locked, PromptTranslation, TaggedItem, TaggitTag, DEFAULT_DB_ALIAS
        )
        self.assertEqual(payload["schema"], "prompt-review-v1")
        self.assertEqual(
            payload["relations"]["author"],
            {"username": "mig-v1-shape-author", "display_name": "Ada Lovelace"},
        )

    def test_frozen_v1_and_v2_fingerprints_differ_for_the_same_content(self):
        author = User.objects.create_user("mig-v1-v2-differ", password="pw")
        prompt = make_prompt(author=author)
        locked = Prompt.objects.select_related("author").get(pk=prompt.pk)
        v1 = _migration._fingerprint(
            _migration._build_v1_payload(locked, PromptTranslation, TaggedItem, TaggitTag, DEFAULT_DB_ALIAS)
        )
        v2 = _migration._fingerprint(
            _migration._build_v2_payload(locked, PromptTranslation, TaggedItem, TaggitTag, DEFAULT_DB_ALIAS)
        )
        self.assertNotEqual(v1, v2)


class NoForbiddenInternalsTests(MigrationTestCase):
    def test_migration_imports_no_runtime_app_modules(self):
        import ast

        source = open(_migration.__file__, encoding="utf-8").read()
        tree = ast.parse(source)
        forbidden_modules = (
            "prompts.models",
            "prompts.review_payload",
            "prompts.review_submission",
            "prompts.review_approval",
            "core.review_binding",
            "core.review_invalidation",
        )
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if any(module == forbidden or module.startswith(forbidden + ".") for forbidden in forbidden_modules):
                    offenders.append(module)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if any(alias.name == forbidden or alias.name.startswith(forbidden + ".") for forbidden in forbidden_modules):
                        offenders.append(alias.name)
        self.assertEqual(offenders, [])

    def test_migration_uses_no_revision_ordering_heuristics(self):
        source = open(_migration.__file__, encoding="utf-8").read()
        for forbidden in ("Revision.objects.first(", "Revision.objects.last(", '.order_by("-id")', ".order_by('-id')", "max(id"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_migration_never_calls_save(self):
        """Every write must be a bulk ``QuerySet.update()`` - a bare
        ``.save()`` call anywhere in the migration's actual code (not its
        prose docstrings, which describe the real primitives' own ``save()``
        contract for context) would risk firing signals and creating an
        unwanted revision."""
        import ast

        source = open(_migration.__file__, encoding="utf-8").read()
        tree = ast.parse(source)
        offenders = [
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "save"
        ]
        self.assertEqual(offenders, [])


class NoReverseOperationTests(MigrationTestCase):
    def test_the_operation_has_no_reverse_code(self):
        operation = _migration.Migration.operations[0]
        self.assertIsNone(operation.reverse_code)

    def test_reversing_raises_not_implemented_error(self):
        """``RunPython.database_backwards()`` refuses outright when
        ``reverse_code`` is ``None`` - checked before it ever touches
        ``schema_editor`` or the historical app registry, so this never
        silently "succeeds" as a no-op the way ``RunPython.noop`` would."""
        operation = _migration.Migration.operations[0]
        with self.assertRaises(NotImplementedError):
            operation.database_backwards("prompts", None, None, None)


# ======================================================================
# Functional migration: preserve-or-invalidate matrix
# ======================================================================


class AlreadyMigratedTests(MigrationTestCase):
    """A row whose stored fingerprint already equals the current v2 payload's
    fingerprint must be a complete, byte-for-byte no-op."""

    def test_review_row_already_on_v2_is_untouched(self):
        prompt = submitted(actor=self.actor)
        before = snapshot(prompt.pk)
        run_migration_via_executor()
        self.assertEqual(snapshot(prompt.pk), before)

    def test_approved_row_already_on_v2_is_untouched(self):
        prompt = approved(actor=self.actor)
        before = snapshot(prompt.pk)
        run_migration_via_executor()
        self.assertEqual(snapshot(prompt.pk), before)


class UnchangedV1BindingTests(MigrationTestCase):
    """A row whose stored fingerprint matches the *current* content under the
    frozen v1 contract: only the fingerprint column may change."""

    def test_review_row_gets_only_its_fingerprint_relabelled(self):
        prompt = submitted(actor=self.actor, author=User.objects.create_user("mig-unchanged-r", password="pw"))
        v1_fingerprint = backdate_to_v1(prompt.pk)
        before = snapshot(prompt.pk)

        run_migration_via_executor()

        after = snapshot(prompt.pk)
        self.assertNotEqual(before["review_payload_fingerprint"], after["review_payload_fingerprint"])
        self.assertEqual(after["review_payload_fingerprint"], fingerprint_review_payload(build_prompt_review_payload(refetch(prompt.pk))))
        self.assertNotEqual(after["review_payload_fingerprint"], v1_fingerprint)
        for key in before:
            if key == "review_payload_fingerprint":
                continue
            with self.subTest(field=key):
                self.assertEqual(before[key], after[key])

    def test_approved_row_gets_only_its_fingerprint_relabelled(self):
        prompt = approved(actor=self.actor, author=User.objects.create_user("mig-unchanged-a", password="pw"))
        backdate_to_v1(prompt.pk)
        before = snapshot(prompt.pk)

        run_migration_via_executor()

        after = snapshot(prompt.pk)
        self.assertEqual(after["status"], "approved")
        self.assertEqual(after["approved_revision_id"], before["approved_revision_id"])
        self.assertEqual(after["review_revision_id"], before["review_revision_id"])
        self.assertEqual(after["reviewed_by_id"], before["reviewed_by_id"])
        self.assertEqual(after["reviewed_at"], before["reviewed_at"])
        self.assertEqual(after["submitted_for_review_at"], before["submitted_for_review_at"])
        self.assertEqual(after["updated_at"], before["updated_at"], "must not bump updated_at")
        self.assertNotEqual(after["review_payload_fingerprint"], before["review_payload_fingerprint"])

    def test_no_author_case_also_relabels_cleanly(self):
        prompt = submitted(actor=self.actor, author=None)
        backdate_to_v1(prompt.pk)
        before = snapshot(prompt.pk)
        run_migration_via_executor()
        after = snapshot(prompt.pk)
        self.assertEqual(before["status"], after["status"])
        self.assertNotEqual(before["review_payload_fingerprint"], after["review_payload_fingerprint"])


class StaleContentInvalidationTests(MigrationTestCase):
    """Any real content drift since the (simulated v1) submit must fail-closed
    invalidate - never silently relabel."""

    def _assert_invalidated(self, pk, before):
        after = snapshot(pk)
        self.assertEqual(after["status"], "draft" if not before["live_i18n"] else "rework")
        self.assertIsNone(after["review_revision_id"])
        self.assertIsNone(after["approved_revision_id"])
        self.assertEqual(after["review_payload_fingerprint"], "")
        self.assertIsNone(after["reviewed_by_id"])
        self.assertIsNone(after["reviewed_at"])
        self.assertIsNone(after["submitted_for_review_at"])
        self.assertGreaterEqual(after["updated_at"], before["updated_at"])
        # content itself is never reverted
        self.assertEqual(after["review_note"], before["review_note"])

    def test_translation_changed_since_submit_is_invalidated(self):
        prompt = submitted(actor=self.actor)
        backdate_to_v1(prompt.pk)
        before = snapshot(prompt.pk)
        PromptTranslation.objects.filter(master_id=prompt.pk, language_code="en").update(title="Changed after submit")
        run_migration_via_executor()
        self._assert_invalidated(prompt.pk, before)

    def test_tag_added_since_submit_is_invalidated(self):
        prompt = submitted(actor=self.actor, tags=("kept-tag",))
        backdate_to_v1(prompt.pk)
        before = snapshot(prompt.pk)
        prompt.tags.add("newly-added-tag")
        run_migration_via_executor()
        self._assert_invalidated(prompt.pk, before)

    def test_tool_removed_since_submit_is_invalidated(self):
        tool = make_tool("Removable Tool")
        prompt = submitted(actor=self.actor, tools=(tool,))
        backdate_to_v1(prompt.pk)
        before = snapshot(prompt.pk)
        prompt.tools.remove(tool)
        run_migration_via_executor()
        self._assert_invalidated(prompt.pk, before)

    def test_author_reassigned_since_submit_is_invalidated(self):
        author_a = User.objects.create_user("mig-stale-author-a", password="pw")
        author_b = User.objects.create_user("mig-stale-author-b", password="pw")
        prompt = submitted(actor=self.actor, author=author_a)
        backdate_to_v1(prompt.pk)
        before = snapshot(prompt.pk)
        Prompt.objects.filter(pk=prompt.pk).update(author=author_b)
        run_migration_via_executor()
        self._assert_invalidated(prompt.pk, before)

    def test_target_status_is_rework_when_a_live_snapshot_exists(self):
        prompt = submitted(actor=self.actor)
        Prompt.objects.filter(pk=prompt.pk).update(live_i18n={"en": {"title": "Live snapshot"}})
        backdate_to_v1(prompt.pk)
        before = snapshot(prompt.pk)
        PromptTranslation.objects.filter(master_id=prompt.pk, language_code="en").update(title="Changed")
        run_migration_via_executor()
        after = snapshot(prompt.pk)
        self.assertEqual(after["status"], "rework")
        self.assertEqual(after["live_i18n"], before["live_i18n"])


class HistoricalNameChangeBoundaryTests(MigrationTestCase):
    """The explicit, conservative, one-time migration boundary: a pure
    display-name or username change since the (simulated v1) submit is
    invalidated during THIS migration - even though, after migration, the
    same kind of change would never invalidate anything again."""

    def test_first_name_change_since_submit_is_invalidated(self):
        author = User.objects.create_user("mig-boundary-first", password="pw", first_name="Old")
        prompt = submitted(actor=self.actor, author=author)
        backdate_to_v1(prompt.pk)
        User.objects.filter(pk=author.pk).update(first_name="New")
        run_migration_via_executor()
        after = snapshot(prompt.pk)
        self.assertEqual(after["status"], "draft")
        self.assertEqual(after["review_payload_fingerprint"], "")

    def test_username_change_since_submit_is_invalidated(self):
        author = User.objects.create_user("mig-boundary-username-old", password="pw")
        prompt = submitted(actor=self.actor, author=author)
        backdate_to_v1(prompt.pk)
        User.objects.filter(pk=author.pk).update(username="mig-boundary-username-new")
        run_migration_via_executor()
        after = snapshot(prompt.pk)
        self.assertEqual(after["status"], "draft")
        self.assertIsNone(after["review_revision_id"])

    def test_after_migration_a_later_name_change_no_longer_invalidates_anything(self):
        """Confirms the boundary is one-time: once this row is on v2 (either
        because it was already v2, or because it just got relabelled),
        subsequent name churn is invisible to the runtime contract."""
        author = User.objects.create_user("mig-boundary-after", password="pw", first_name="Stable")
        prompt = submitted(actor=self.actor, author=author)
        backdate_to_v1(prompt.pk)
        run_migration_via_executor()
        migrated = snapshot(prompt.pk)
        self.assertEqual(migrated["status"], "review")

        fingerprint_before = fingerprint_review_payload(build_prompt_review_payload(refetch(prompt.pk)))
        User.objects.filter(pk=author.pk).update(first_name="Changed-Post-Migration")
        fingerprint_after = fingerprint_review_payload(build_prompt_review_payload(refetch(prompt.pk)))
        self.assertEqual(fingerprint_before, fingerprint_after)


class CorruptBindingInvalidationTests(MigrationTestCase):
    """Structurally invalid bindings must invalidate regardless of whether
    the content itself ever changed - there is nothing valid to compare
    against."""

    def test_missing_review_revision_is_invalidated(self):
        prompt = submitted(actor=self.actor)
        Prompt.objects.filter(pk=prompt.pk).update(review_revision=None)
        run_migration_via_executor()
        after = snapshot(prompt.pk)
        self.assertEqual(after["status"], "draft")
        self.assertEqual(after["review_payload_fingerprint"], "")

    def test_empty_fingerprint_is_invalidated(self):
        prompt = submitted(actor=self.actor)
        Prompt.objects.filter(pk=prompt.pk).update(review_payload_fingerprint="")
        run_migration_via_executor()
        self.assertEqual(snapshot(prompt.pk)["status"], "draft")

    def test_malformed_fingerprint_is_invalidated(self):
        prompt = submitted(actor=self.actor)
        Prompt.objects.filter(pk=prompt.pk).update(review_payload_fingerprint="NOT-A-VALID-HEX-DIGEST")
        run_migration_via_executor()
        self.assertEqual(snapshot(prompt.pk)["status"], "draft")

    def test_uppercase_hex_fingerprint_is_invalidated(self):
        prompt = submitted(actor=self.actor)
        current = Prompt.objects.get(pk=prompt.pk).review_payload_fingerprint
        Prompt.objects.filter(pk=prompt.pk).update(review_payload_fingerprint=current.upper())
        run_migration_via_executor()
        self.assertEqual(snapshot(prompt.pk)["status"], "draft")

    def test_revision_not_containing_this_prompt_is_invalidated(self):
        prompt = submitted(actor=self.actor)
        foreign = unrelated_revision()
        Prompt.objects.filter(pk=prompt.pk).update(review_revision=foreign)
        run_migration_via_executor()
        after = snapshot(prompt.pk)
        self.assertEqual(after["status"], "draft")
        self.assertIsNone(after["review_revision_id"])

    def test_review_status_with_approved_revision_already_set_is_invalidated(self):
        """The C3A-specific pre-state check: a `review` row must never already
        carry an `approved_revision` - not part of the shared B2B1 validator,
        applied only to `review`-status rows."""
        prompt = approved(actor=self.actor)
        Prompt.objects.filter(pk=prompt.pk).update(status=Workflow.STATUS_REVIEW)
        run_migration_via_executor()
        after = snapshot(prompt.pk)
        self.assertEqual(after["status"], "draft")
        self.assertIsNone(after["approved_revision_id"])
        self.assertIsNone(after["review_revision_id"])

    def test_approved_missing_approved_revision_is_invalidated(self):
        prompt = approved(actor=self.actor)
        Prompt.objects.filter(pk=prompt.pk).update(approved_revision=None)
        run_migration_via_executor()
        self.assertEqual(snapshot(prompt.pk)["status"], "draft")

    def test_approved_with_mismatched_approved_revision_is_invalidated(self):
        prompt = approved(actor=self.actor)
        foreign = unrelated_revision()
        Prompt.objects.filter(pk=prompt.pk).update(approved_revision=foreign)
        run_migration_via_executor()
        after = snapshot(prompt.pk)
        self.assertEqual(after["status"], "draft")
        self.assertIsNone(after["approved_revision_id"])
        self.assertIsNone(after["review_revision_id"])


# ======================================================================
# Untouched statuses
# ======================================================================


class UntouchedStatusTests(MigrationTestCase):
    """draft/rework/published/archived are never selected in the first
    place - not even a stray, leftover binding-shaped value should provoke a
    write."""

    def _make_row(self, status, **extra):
        prompt = make_prompt(status=status, **extra)
        return prompt

    def test_draft_with_no_binding_is_untouched(self):
        prompt = self._make_row(Workflow.STATUS_DRAFT)
        before = snapshot(prompt.pk)
        run_migration_via_executor()
        self.assertEqual(snapshot(prompt.pk), before)

    def test_rework_row_is_untouched(self):
        prompt = self._make_row(Workflow.STATUS_REWORK, live_i18n={"en": {"title": "Live"}})
        before = snapshot(prompt.pk)
        run_migration_via_executor()
        self.assertEqual(snapshot(prompt.pk), before)

    def test_published_row_with_a_leftover_binding_is_untouched(self):
        prompt = approved(actor=self.actor)
        Prompt.objects.filter(pk=prompt.pk).update(
            status=Workflow.STATUS_PUBLISHED,
            is_published=True,
            published_at=timezone.now(),
            live_i18n={"en": {"title": "Published"}},
        )
        before = snapshot(prompt.pk)
        run_migration_via_executor()
        self.assertEqual(snapshot(prompt.pk), before)

    def test_archived_row_with_a_leftover_binding_is_untouched(self):
        prompt = approved(actor=self.actor)
        Prompt.objects.filter(pk=prompt.pk).update(
            status=Workflow.STATUS_ARCHIVED, live_i18n={"en": {"title": "Archived"}}
        )
        before = snapshot(prompt.pk)
        run_migration_via_executor()
        self.assertEqual(snapshot(prompt.pk), before)

    def test_only_review_and_approved_rows_are_ever_examined(self):
        draft = self._make_row(Workflow.STATUS_DRAFT)
        rework = self._make_row(Workflow.STATUS_REWORK)
        review = submitted(actor=self.actor)
        backdate_to_v1(review.pk)  # give the migration something to actually do
        before = {pk: snapshot(pk) for pk in (draft.pk, rework.pk, review.pk)}
        run_migration_via_executor()
        self.assertEqual(snapshot(draft.pk), before[draft.pk])
        self.assertEqual(snapshot(rework.pk), before[rework.pk])
        self.assertNotEqual(
            snapshot(review.pk)["review_payload_fingerprint"],
            before[review.pk]["review_payload_fingerprint"],
        )


# ======================================================================
# No new reversion history, idempotency
# ======================================================================


class NoNewRevisionsTests(MigrationTestCase):
    def test_migration_creates_no_revisions_or_versions(self):
        submitted(actor=self.actor)
        approved(actor=self.actor)
        stale = submitted(actor=self.actor)
        backdate_to_v1(stale.pk)
        PromptTranslation.objects.filter(master_id=stale.pk).update(title="Drifted")

        revisions_before = Revision.objects.count()
        versions_before = Version.objects.count()

        run_migration_via_executor()

        self.assertEqual(Revision.objects.count(), revisions_before)
        self.assertEqual(Version.objects.count(), versions_before)


class IdempotencyTests(MigrationTestCase):
    def test_a_second_run_over_already_migrated_rows_changes_nothing(self):
        v2_review = submitted(actor=self.actor)
        v2_approved = approved(actor=self.actor)
        relabelled = submitted(actor=self.actor, author=User.objects.create_user("mig-idem-relabel", password="pw"))
        backdate_to_v1(relabelled.pk)
        invalidated = submitted(actor=self.actor)
        backdate_to_v1(invalidated.pk)
        PromptTranslation.objects.filter(master_id=invalidated.pk).update(title="Drifted for idempotency")
        untouched_draft = make_prompt(status=Workflow.STATUS_DRAFT)

        run_migration_via_executor()

        pks = [v2_review.pk, v2_approved.pk, relabelled.pk, invalidated.pk, untouched_draft.pk]
        revisions_after_first = Revision.objects.count()
        versions_after_first = Version.objects.count()
        before_second = {pk: snapshot(pk) for pk in pks}

        run_migration_directly()

        for pk in pks:
            with self.subTest(pk=pk):
                self.assertEqual(snapshot(pk), before_second[pk])
        self.assertEqual(Revision.objects.count(), revisions_after_first)
        self.assertEqual(Version.objects.count(), versions_after_first)

    def test_running_through_the_executor_twice_is_also_safe(self):
        prompt = submitted(actor=self.actor)
        backdate_to_v1(prompt.pk)
        run_migration_via_executor()
        after_first = snapshot(prompt.pk)
        run_migration_via_executor()
        after_second = snapshot(prompt.pk)
        self.assertEqual(after_first, after_second)
