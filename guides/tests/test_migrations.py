"""
Tests for guides.migrations.0006_mark_historical_starter_guide, using
Django's own executor-based migration test pattern (no extra dependency).
"""
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class MigrationTestCase(TransactionTestCase):
    app = "guides"
    migrate_from = None
    migrate_to = None

    def setUp(self):
        super().setUp()
        assert self.migrate_from and self.migrate_to, (
            f"{type(self).__name__} must set migrate_from and migrate_to"
        )
        migrate_from = [(self.app, self.migrate_from)]
        migrate_to = [(self.app, self.migrate_to)]

        executor = MigrationExecutor(connection)
        executor.migrate(migrate_from)
        old_apps = executor.loader.project_state(migrate_from).apps

        self.setUpBeforeMigration(old_apps)

        # Reload the executor: the previous migrate() call left its internal
        # graph/state stale for the next migrate() call.
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(migrate_to)

        self.apps = executor.loader.project_state(migrate_to).apps

    def setUpBeforeMigration(self, apps):
        raise NotImplementedError

    def tearDown(self):
        # migrate_to may be an earlier point than HEAD (e.g. a reverse-
        # migration test); restore the full current schema so later tests
        # in this process see the app's real, latest migration state.
        call_command("migrate", self.app, verbosity=0)
        super().tearDown()


def _make_guide_with_translations(apps, *, status="published", translations):
    """translations: list of (language_code, slug) tuples."""
    Guide = apps.get_model("guides", "Guide")
    GuideTranslation = apps.get_model("guides", "GuideTranslation")

    guide = Guide.objects.create(status=status)
    for language_code, slug in translations:
        GuideTranslation.objects.create(
            master=guide,
            language_code=language_code,
            title=f"Title {slug}",
            intro="intro",
            body="body",
            slug=slug,
        )
    return guide


class UniqueHistoricalStarterTests(MigrationTestCase):
    migrate_from = "0005_guide_is_starter"
    migrate_to = "0006_mark_historical_starter_guide"

    def setUpBeforeMigration(self, apps):
        self.starter = _make_guide_with_translations(
            apps, translations=[("en", "start-guide-en"), ("de", "start-guide-de")]
        )
        self.other = _make_guide_with_translations(
            apps, translations=[("en", "some-other-guide")]
        )

    def test_unique_historical_starter_is_marked(self):
        Guide = self.apps.get_model("guides", "Guide")
        self.assertTrue(Guide.objects.get(pk=self.starter.pk).is_starter)
        self.assertFalse(Guide.objects.get(pk=self.other.pk).is_starter)


class MultipleTranslationsOfSameGuideTests(MigrationTestCase):
    """Both start-guide-en and start-guide-de belong to one shared Guide;
    this must be treated as a single starter, not an ambiguity."""

    migrate_from = "0005_guide_is_starter"
    migrate_to = "0006_mark_historical_starter_guide"

    def setUpBeforeMigration(self, apps):
        self.starter = _make_guide_with_translations(
            apps, translations=[("en", "start-guide-en"), ("de", "start-guide-de")]
        )

    def test_single_shared_guide_marked_once(self):
        Guide = self.apps.get_model("guides", "Guide")
        qs = Guide.objects.filter(is_starter=True)
        self.assertEqual(qs.count(), 1)
        self.assertEqual(qs.first().pk, self.starter.pk)


class NoHistoricalStarterTests(MigrationTestCase):
    migrate_from = "0005_guide_is_starter"
    migrate_to = "0006_mark_historical_starter_guide"

    def setUpBeforeMigration(self, apps):
        _make_guide_with_translations(apps, translations=[("en", "unrelated-guide")])

    def test_migration_runs_without_marking_anything(self):
        Guide = self.apps.get_model("guides", "Guide")
        self.assertEqual(Guide.objects.filter(is_starter=True).count(), 0)


class AmbiguousHistoricalStartersTests(TransactionTestCase):
    """Two different shared Guides both match the historical slug prefix:
    the migration must abort instead of guessing."""

    def test_migration_aborts_on_ambiguous_match(self):
        executor = MigrationExecutor(connection)
        executor.migrate([("guides", "0005_guide_is_starter")])
        old_apps = executor.loader.project_state([("guides", "0005_guide_is_starter")]).apps
        Guide = old_apps.get_model("guides", "Guide")

        g1 = _make_guide_with_translations(old_apps, translations=[("en", "start-guide-en")])
        g2 = _make_guide_with_translations(old_apps, translations=[("de", "start-guide-de-legacy")])

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        try:
            with self.assertRaises(RuntimeError):
                executor.migrate([("guides", "0006_mark_historical_starter_guide")])
        finally:
            # Remove the ambiguous fixture data (the actual bug this test
            # exercises) so the app can be restored to its real HEAD state
            # for later tests in this process.
            Guide.objects.filter(pk__in=[g1.pk, g2.pk]).delete()
            call_command("migrate", "guides", verbosity=0)


class ReverseMigrationTests(MigrationTestCase):
    migrate_from = "0006_mark_historical_starter_guide"
    migrate_to = "0005_guide_is_starter"

    def setUpBeforeMigration(self, apps):
        self.starter = _make_guide_with_translations(
            apps, translations=[("en", "start-guide-en"), ("de", "start-guide-de")]
        )
        self.other = _make_guide_with_translations(
            apps, translations=[("en", "some-other-guide")]
        )
        Guide = apps.get_model("guides", "Guide")
        Guide.objects.filter(pk=self.starter.pk).update(is_starter=True)

    def test_reverse_removes_only_the_historical_flag(self):
        Guide = self.apps.get_model("guides", "Guide")
        GuideTranslation = self.apps.get_model("guides", "GuideTranslation")
        self.assertFalse(Guide.objects.get(pk=self.starter.pk).is_starter)
        self.assertTrue(Guide.objects.filter(pk=self.starter.pk).exists())
        self.assertTrue(Guide.objects.filter(pk=self.other.pk).exists())
        self.assertEqual(
            GuideTranslation.objects.filter(master_id=self.starter.pk).count(), 2
        )
        self.assertEqual(
            set(GuideTranslation.objects.filter(master_id=self.starter.pk).values_list("slug", flat=True)),
            {"start-guide-en", "start-guide-de"},
        )
