"""
Confirms that switching updated_at to auto_now=True (Beta 8.6) is a
schema-only change: pre-existing historical values are not rewritten by
the AlterField migration itself, for all four editorial models.
"""
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.test import TransactionTestCase
from django.utils import timezone


class UpdatedAtMigrationPreservationMixin:
    """Migrates to just before the auto_now AlterField, sets a historical
    updated_at via the pre-migration historical model, migrates forward,
    and asserts the stored value is untouched."""

    app = None
    migrate_from = None
    migrate_to = None

    def test_historical_value_survives_the_alter_field(self):
        migrate_from = [(self.app, self.migrate_from)]
        migrate_to = [(self.app, self.migrate_to)]

        if self.app == "prompts":
            # Beta 11.11C4D added prompts/migrations/0008_prompt_review_payload_v2.py,
            # which is deliberately irreversible (no reverse_code - see its own
            # module docstring). Rolling "prompts" back below it would otherwise
            # unapply 0008 first and raise IrreversibleError. It has zero schema
            # operations, so deleting its own "applied" bookkeeping row (never
            # touching data, never invoking reverse_code) is a safe stand-in; the
            # final `call_command("migrate", ...)` below re-runs it forward again.
            MigrationRecorder(connection).record_unapplied(
                "prompts", "0008_prompt_review_payload_v2"
            )

        executor = MigrationExecutor(connection)
        executor.migrate(migrate_from)
        old_apps = executor.loader.project_state(migrate_from).apps
        Model = old_apps.get_model(self.app, self.model_name)

        fixed = timezone.now() - timezone.timedelta(days=365)
        obj = Model.objects.create(status="draft", updated_at=fixed)

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(migrate_to)

        new_apps = executor.loader.project_state(migrate_to).apps
        NewModel = new_apps.get_model(self.app, self.model_name)
        self.assertEqual(NewModel.objects.get(pk=obj.pk).updated_at, fixed)

        # Restore full HEAD schema for later tests sharing this test DB.
        call_command("migrate", self.app, verbosity=0)


class GuideUpdatedAtMigrationTests(UpdatedAtMigrationPreservationMixin, TransactionTestCase):
    app = "guides"
    model_name = "Guide"
    migrate_from = "0007_guide_starter_uniqueness"
    migrate_to = "0008_alter_guide_updated_at"


class PromptUpdatedAtMigrationTests(UpdatedAtMigrationPreservationMixin, TransactionTestCase):
    app = "prompts"
    model_name = "Prompt"
    migrate_from = "0005_alter_prompt_updated_at"
    migrate_to = "0006_alter_prompt_updated_at"


class UseCaseUpdatedAtMigrationTests(UpdatedAtMigrationPreservationMixin, TransactionTestCase):
    app = "usecases"
    model_name = "UseCase"
    migrate_from = "0004_alter_usecase_updated_at"
    migrate_to = "0005_alter_usecase_updated_at"


class ComparisonUpdatedAtMigrationTests(UpdatedAtMigrationPreservationMixin, TransactionTestCase):
    app = "compare"
    model_name = "Comparison"
    migrate_from = "0005_alter_comparison_options_and_more"
    migrate_to = "0006_alter_comparison_updated_at"
