"""
Beta 11.11C4E: the backfill half of
``prompts/migrations/0009_prompt_live_author_snapshot.py``, driven through
Django's real ``MigrationExecutor`` against PostgreSQL - the project's
established pattern for data-migration tests (see
``guides/tests/test_migrations.py`` and
``core/tests/test_editorial_review_binding_migration.py``).

Unlike Beta 11.11C4D's ``0008`` migration, ``0009`` is genuinely reversible
(``AddField`` reverses to ``RemoveField``, and the backfill's own
``reverse_code`` is a real, honest ``RunPython.noop`` - see the migration's
own module docstring for why that is not a "seemingly safe" shortcut here).
Rolling "prompts" back to just before ``0009`` therefore needs none of C4D's
``MigrationRecorder.record_unapplied()`` workaround for ``0008`` itself - only
``0009`` is ever unapplied and reapplied in this module.
"""
import importlib

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from reversion.models import Revision, Version

from prompts.models import PROMPT_AUTHOR_SNAPSHOT_SCHEMA, Prompt

User = get_user_model()

_migration = importlib.import_module("prompts.migrations.0009_prompt_live_author_snapshot")

BEFORE = [("prompts", "0008_prompt_review_payload_v2")]
AFTER = [("prompts", "0009_prompt_live_author_snapshot")]

SNAPSHOT = {"en": {"title": "Published title"}}


class MigrationTestCase(TransactionTestCase):
    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(BEFORE)
        self.old_apps = executor.loader.project_state(BEFORE).apps

        self.seeded = self.seed(self.old_apps)

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(AFTER)
        self.new_apps = executor.loader.project_state(AFTER).apps

    def tearDown(self):
        call_command("migrate", "prompts", verbosity=0)
        super().tearDown()

    def seed(self, apps):
        return {}

    def historical_prompt(self, apps=None):
        return (apps or self.old_apps).get_model("prompts", "Prompt")

    def make_row(self, apps, *, author=None, live_i18n=None, **extra):
        Prompt = self.historical_prompt(apps)
        defaults = {"live_i18n": {} if live_i18n is None else live_i18n, "author": author}
        defaults.update(extra)
        return Prompt.objects.using(connection.alias).create(**defaults).pk

    def reload(self, pk):
        return self.new_apps.get_model("prompts", "Prompt").objects.get(pk=pk)


class BackfillMatrixTests(MigrationTestCase):
    def seed(self, apps):
        User = apps.get_model("auth", "User")
        author_full = User.objects.create(
            username="mig-snap-full", first_name="Jane", last_name="Doe"
        )
        author_username_only = User.objects.create(username="mig-snap-usernameonly")

        return {
            "with_snapshot_full_name": self.make_row(
                apps, author=author_full, live_i18n=SNAPSHOT
            ),
            "with_snapshot_username_fallback": self.make_row(
                apps, author=author_username_only, live_i18n=SNAPSHOT
            ),
            "with_snapshot_no_author": self.make_row(apps, author=None, live_i18n=SNAPSHOT),
            "without_snapshot": self.make_row(apps, author=author_full, live_i18n={}),
        }

    def test_full_name_author_gets_the_full_name(self):
        row = self.reload(self.seeded["with_snapshot_full_name"])
        self.assertEqual(
            row.live_author, {"schema": PROMPT_AUTHOR_SNAPSHOT_SCHEMA, "display_name": "Jane Doe"}
        )

    def test_nameless_author_falls_back_to_username(self):
        row = self.reload(self.seeded["with_snapshot_username_fallback"])
        self.assertEqual(
            row.live_author,
            {"schema": PROMPT_AUTHOR_SNAPSHOT_SCHEMA, "display_name": "mig-snap-usernameonly"},
        )

    def test_no_author_gets_an_explicit_empty_snapshot(self):
        row = self.reload(self.seeded["with_snapshot_no_author"])
        self.assertEqual(
            row.live_author, {"schema": PROMPT_AUTHOR_SNAPSHOT_SCHEMA, "display_name": ""}
        )

    def test_no_content_live_snapshot_means_no_author_snapshot_either(self):
        row = self.reload(self.seeded["without_snapshot"])
        self.assertIsNone(row.live_author)


class StatusMatrixTests(MigrationTestCase):
    """Backfill scope is bool(live_i18n), never status - covers every status
    that can plausibly carry a leftover content snapshot."""

    def seed(self, apps):
        return {
            "published": self.make_row(apps, status="published", live_i18n=SNAPSHOT, is_published=True),
            "archived": self.make_row(apps, status="archived", live_i18n=SNAPSHOT),
            "rework": self.make_row(apps, status="rework", live_i18n=SNAPSHOT),
            "approved": self.make_row(apps, status="approved", live_i18n=SNAPSHOT),
            "review": self.make_row(apps, status="review", live_i18n=SNAPSHOT),
            "draft_no_snapshot": self.make_row(apps, status="draft", live_i18n={}),
        }

    def test_snapshot_created_regardless_of_status(self):
        for key in ("published", "archived", "rework", "approved", "review"):
            with self.subTest(status=key):
                row = self.reload(self.seeded[key])
                self.assertIsNotNone(row.live_author)
                self.assertEqual(row.live_author["schema"], PROMPT_AUTHOR_SNAPSHOT_SCHEMA)

    def test_status_itself_is_never_changed(self):
        for key in ("published", "archived", "rework", "approved", "review", "draft_no_snapshot"):
            with self.subTest(status=key):
                self.assertEqual(self.reload(self.seeded[key]).status, key.split("_")[0])

    def test_draft_without_snapshot_gets_no_author_snapshot(self):
        row = self.reload(self.seeded["draft_no_snapshot"])
        self.assertIsNone(row.live_author)


class PreservedFieldsTests(MigrationTestCase):
    def seed(self, apps):
        User = apps.get_model("auth", "User")
        reviewer = User.objects.create(username="mig-snap-reviewer")
        author = User.objects.create(username="mig-snap-preserve-author", first_name="Preserve")
        Revision_ = self.old_apps.get_model("reversion", "Revision")
        from django.utils import timezone

        revision = Revision_.objects.using(connection.alias).create(
            date_created=timezone.now(), comment="preserve-me"
        )
        return {
            "row": self.make_row(
                apps,
                author=author,
                live_i18n=SNAPSHOT,
                status="approved",
                review_revision=revision,
                approved_revision=revision,
                review_payload_fingerprint="a" * 64,
                reviewed_by=reviewer,
                review_note="keep me exactly as I am",
                last_published_revision_id=999,
                is_published=False,
            )
        }

    def test_workflow_and_binding_fields_are_untouched(self):
        row = self.reload(self.seeded["row"])
        self.assertEqual(row.status, "approved")
        self.assertIsNotNone(row.review_revision_id)
        self.assertIsNotNone(row.approved_revision_id)
        self.assertEqual(row.review_revision_id, row.approved_revision_id)
        self.assertEqual(row.review_payload_fingerprint, "a" * 64)
        self.assertIsNotNone(row.reviewed_by_id)
        self.assertEqual(row.review_note, "keep me exactly as I am")
        self.assertEqual(row.last_published_revision_id, 999)
        self.assertEqual(row.live_i18n, SNAPSHOT)
        self.assertFalse(row.is_published)

    def test_author_id_is_untouched(self):
        row = self.reload(self.seeded["row"])
        self.assertIsNotNone(row.author_id)

    def test_live_author_was_still_added(self):
        row = self.reload(self.seeded["row"])
        self.assertEqual(row.live_author["display_name"], "Preserve")

    def test_updated_at_is_not_bumped(self):
        pk = self.seeded["row"]
        before = self.historical_prompt(self.old_apps).objects.using(connection.alias).get(pk=pk).updated_at
        after = self.reload(pk).updated_at
        self.assertEqual(before, after)


class _StubSchemaEditor:
    connection = connection


class IdempotencyTests(MigrationTestCase):
    """``live_author`` does not exist yet at the BEFORE (0008) schema, so a
    "pre-existing" stored value can only be injected *after* ``setUp``'s real
    first backfill has already run and the column exists - via the AFTER
    (0009) historical model - before re-invoking the backfill function a
    second, direct time."""

    def seed(self, apps):
        User = apps.get_model("auth", "User")
        author = User.objects.create(username="mig-snap-idem-author", first_name="Idem")
        return {
            "already_valid": self.make_row(apps, author=author, live_i18n=SNAPSHOT),
            "no_snapshot": self.make_row(apps, live_i18n={}),
        }

    def test_already_valid_snapshot_is_left_completely_untouched(self):
        """Even though the author's *current* name differs from the stored
        snapshot, an already well-formed v1 snapshot must never be
        overwritten by a repeat run - only a conscious republish may replace
        it (runtime contract), and a migration re-run must be exactly as
        conservative."""
        pk = self.seeded["already_valid"]
        NewPrompt = self.new_apps.get_model("prompts", "Prompt")
        NewPrompt.objects.filter(pk=pk).update(
            live_author={"schema": PROMPT_AUTHOR_SNAPSHOT_SCHEMA, "display_name": "Stale But Valid"}
        )

        _migration.backfill_live_author_snapshots(self.new_apps, _StubSchemaEditor())

        self.assertEqual(self.reload(pk).live_author["display_name"], "Stale But Valid")

    def test_a_second_direct_backfill_invocation_changes_nothing(self):
        pk = self.seeded["already_valid"]
        before = self.reload(pk).live_author

        _migration.backfill_live_author_snapshots(self.new_apps, _StubSchemaEditor())

        after = self.reload(pk).live_author
        self.assertEqual(before, after)

    def test_no_snapshot_row_stays_none_across_repeat_runs(self):
        pk = self.seeded["no_snapshot"]

        _migration.backfill_live_author_snapshots(self.new_apps, _StubSchemaEditor())
        self.assertIsNone(self.reload(pk).live_author)

    def test_malformed_snapshot_with_a_live_i18n_is_deterministically_replaced(self):
        """Controlled-repeat-run contract (Phase 13): an unknown/malformed
        stored value on a row that does have a content live snapshot is
        replaced by the current v1 snapshot - never left as-is, and never an
        exception because of user-editable JSON."""
        pk = self.seeded["already_valid"]
        NewPrompt = self.new_apps.get_model("prompts", "Prompt")
        NewPrompt.objects.filter(pk=pk).update(live_author={"unexpected": "shape"})

        _migration.backfill_live_author_snapshots(self.new_apps, _StubSchemaEditor())

        row = self.reload(pk)
        self.assertEqual(
            row.live_author, {"schema": PROMPT_AUTHOR_SNAPSHOT_SCHEMA, "display_name": "Idem"}
        )


class NoReversionSideEffectTests(MigrationTestCase):
    def seed(self, apps):
        User = apps.get_model("auth", "User")
        author = User.objects.create(username="mig-snap-norevision", first_name="No", last_name="Revision")
        return {
            "a": self.make_row(apps, author=author, live_i18n=SNAPSHOT),
            "b": self.make_row(apps, live_i18n=SNAPSHOT),
            "c": self.make_row(apps, live_i18n={}),
        }

    def test_migration_creates_no_revisions_or_versions(self):
        # Re-run the forward function directly to reproduce the exact counts
        # the executor's own forward pass already produced, without needing
        # to unapply/reapply again.
        revisions_before = Revision.objects.count()
        versions_before = Version.objects.count()

        _migration.backfill_live_author_snapshots(self.new_apps, _StubSchemaEditor())

        self.assertEqual(Revision.objects.count(), revisions_before)
        self.assertEqual(Version.objects.count(), versions_before)


class TranslationsUntouchedTests(MigrationTestCase):
    def seed(self, apps):
        PromptTranslation = apps.get_model("prompts", "PromptTranslation")
        pk = self.make_row(apps, live_i18n=SNAPSHOT)
        prompt = self.historical_prompt(apps).objects.using(connection.alias).get(pk=pk)
        PromptTranslation.objects.using(connection.alias).create(
            master=prompt, language_code="en", title="Untouched Title",
            intro="i", body="b", outro="o", slug="mig-snap-translation-en",
        )
        return {"pk": pk}

    def test_translations_survive_the_migration_unchanged(self):
        PromptTranslation = self.new_apps.get_model("prompts", "PromptTranslation")
        translation = PromptTranslation.objects.get(master_id=self.seeded["pk"], language_code="en")
        self.assertEqual(translation.title, "Untouched Title")


class FrozenBuilderParityTests(MigrationTestCase):
    """The migration's frozen local builder must agree with the runtime
    ``Prompt._build_live_author_snapshot()`` for the same author state - this
    is a test-only import of the runtime builder, never done inside the
    migration itself (see its module docstring and the static checks below)."""

    def seed(self, apps):
        return {}

    def test_full_name_case_matches_runtime(self):
        migration_result = _migration._build_snapshot(
            has_author=True, first_name="Ada", last_name="Lovelace", username="ada"
        )
        runtime_author = User(first_name="Ada", last_name="Lovelace", username="ada")
        runtime_result = Prompt(author=runtime_author)._build_live_author_snapshot()
        self.assertEqual(migration_result, runtime_result)

    def test_username_fallback_case_matches_runtime(self):
        migration_result = _migration._build_snapshot(
            has_author=True, first_name="", last_name="", username="onlyusername"
        )
        runtime_author = User(first_name="", last_name="", username="onlyusername")
        runtime_result = Prompt(author=runtime_author)._build_live_author_snapshot()
        self.assertEqual(migration_result, runtime_result)

    def test_no_author_case_matches_runtime(self):
        migration_result = _migration._build_snapshot(
            has_author=False, first_name=None, last_name=None, username=None
        )
        runtime_prompt = Prompt(author=None)
        runtime_result = runtime_prompt._build_live_author_snapshot()
        self.assertEqual(migration_result, runtime_result)


class ArchitectureStaticTests(MigrationTestCase):
    def seed(self, apps):
        return {}

    def test_migration_has_a_real_reverse(self):
        add_field, run_python = _migration.Migration.operations
        self.assertIsNotNone(run_python.reverse_code)

    def test_migration_imports_no_runtime_modules(self):
        import ast

        source = open(_migration.__file__, encoding="utf-8").read()
        tree = ast.parse(source)
        forbidden = (
            "prompts.models",
            "prompts.review_payload",
            "prompts.review_submission",
            "prompts.review_approval",
            "core.models.editorial",
            "core.review_binding",
            "core.review_invalidation",
        )
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if any(module == f or module.startswith(f + ".") for f in forbidden):
                    offenders.append(module)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if any(alias.name == f or alias.name.startswith(f + ".") for f in forbidden):
                        offenders.append(alias.name)
        self.assertEqual(offenders, [])

    def test_migration_never_calls_save(self):
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

    def test_migration_depends_on_0008(self):
        self.assertIn(("prompts", "0008_prompt_review_payload_v2"), _migration.Migration.dependencies)
