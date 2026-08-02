"""
Beta 11.9: the wiring of the comparison live-entries schema migration.

The snapshot behaviour itself is covered in
``compare/tests/test_live_entries.py``. This module asserts the properties
that only matter when Django actually applies the migration: that it is
registered, is a pure schema addition, and adds exactly the nullable field
the State-A/State-C contract needs.

There is deliberately no data migration: entry *content* was never captured
by django-reversion (``ComparisonToolEntryTranslation`` is not registered,
and every recorded entry version carries only ``comparison``/``position``/
``tool``), so no published entry state could be reconstructed for existing
records. Instead ``live_entries IS NULL`` marks such records as legacy and
``ComparisonQuerySet.visible_on_site()`` keeps them on their pre-11.9
behaviour until their next publish writes a real snapshot.
"""
from importlib import import_module

from django.db import migrations, models
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase

from compare.models import Comparison

MIGRATION_NAME = "0007_comparison_live_entries"

migration_module = import_module(f"compare.migrations.{MIGRATION_NAME}")


class LiveEntriesMigrationWiringTests(TestCase):
    def setUp(self):
        self.migration = migration_module.Migration(MIGRATION_NAME, "compare")

    def test_migration_is_discovered_by_the_loader(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        self.assertIn(("compare", MIGRATION_NAME), loader.disk_migrations)

    def test_it_only_adds_the_live_entries_field(self):
        self.assertEqual(len(self.migration.operations), 1)
        operation = self.migration.operations[0]
        self.assertIsInstance(operation, migrations.AddField)
        self.assertEqual(operation.model_name, "comparison")
        self.assertEqual(operation.name, "live_entries")

    def test_it_declares_no_data_migration(self):
        for operation in self.migration.operations:
            with self.subTest(operation=type(operation).__name__):
                self.assertNotIsInstance(operation, migrations.RunPython)

    def test_it_follows_the_previous_compare_migration(self):
        self.assertIn(("compare", "0006_alter_comparison_updated_at"), self.migration.dependencies)

    def test_the_field_is_nullable_so_legacy_records_are_distinguishable(self):
        """NULL is the State-C marker; ``[]`` means "published with no
        entries". Both must be representable."""
        field = Comparison._meta.get_field("live_entries")
        self.assertIsInstance(field, models.JSONField)
        self.assertTrue(field.null)
        self.assertIsNone(field.default)
