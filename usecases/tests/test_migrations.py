"""
Beta 11.7A: the wiring of the use-case backfill migration.

The algorithm itself is covered in
``usecases/tests/test_live_visibility_legacy.py``, which runs the forward
function against real reversion data. This module asserts the properties
that only matter when Django actually applies it: that it is registered, is
a pure data migration, declares the dependencies its queries need, and is
safely reversible.
"""
from importlib import import_module

from django.db import migrations
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase

MIGRATION_NAME = "0006_backfill_usecase_live_state"

migration_module = import_module(f"usecases.migrations.{MIGRATION_NAME}")


class BackfillMigrationWiringTests(TestCase):
    def setUp(self):
        self.migration = migration_module.Migration(MIGRATION_NAME, "usecases")

    def test_migration_is_discovered_by_the_loader(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        self.assertIn(("usecases", MIGRATION_NAME), loader.disk_migrations)

    def test_it_is_a_pure_data_migration(self):
        """No schema operation - Beta 11.7A must not alter the database
        structure, only fill in values."""
        self.assertTrue(self.migration.operations)
        for operation in self.migration.operations:
            with self.subTest(operation=type(operation).__name__):
                self.assertIsInstance(operation, migrations.RunPython)

    def test_it_declares_the_dependencies_its_queries_need(self):
        """It reads reversion.Version and contenttypes.ContentType, so both
        must already exist when it runs."""
        apps_depended_on = {app for app, _name in self.migration.dependencies}
        self.assertIn("usecases", apps_depended_on)
        self.assertIn("reversion", apps_depended_on)
        self.assertIn("contenttypes", apps_depended_on)

    def test_it_follows_the_previous_usecases_migration(self):
        self.assertIn(
            ("usecases", "0005_alter_usecase_updated_at"), self.migration.dependencies
        )

    def test_reverse_is_an_explicit_noop(self):
        """Reversing cannot distinguish a backfilled value from one an editor
        published afterwards, so it deliberately does nothing rather than
        deleting data."""
        operation = self.migration.operations[0]
        self.assertIs(operation.reverse_code, migrations.RunPython.noop)

    def test_it_is_reversible(self):
        self.assertTrue(self.migration.operations[0].reversible)

    def test_forward_function_is_the_documented_entry_point(self):
        self.assertIs(
            self.migration.operations[0].code, migration_module.backfill_live_state
        )
