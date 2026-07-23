"""
Beta 11.7A: the legacy backfill in
``usecases/migrations/0006_backfill_usecase_live_state.py``.

The migration's forward function is imported and run directly against
``django.apps.apps`` and a real schema editor. That exercises the actual
algorithm - the same reversion lookups, the same fail-closed branches -
while letting each test build the precise legacy shape it needs.

Every test builds its reversion history the way the application does: inside
``reversion.create_revision()``, so the use case and its translations land in
one revision (``reversion.register(UseCase, follow=("translations",))``).
"""
import copy
import json
from importlib import import_module

import reversion
from django.apps import apps as global_apps
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import TestCase
from django.utils import timezone, translation
from reversion.models import Version

from core.models.editorial import EditorialWorkflowMixin
from usecases.models import UseCase

migration = import_module("usecases.migrations.0006_backfill_usecase_live_state")
backfill_live_state = migration.backfill_live_state

UseCaseTranslation = UseCase._meta.get_field("translations").related_model


def run_backfill():
    """Run the migration's forward function exactly as `migrate` would."""
    with connection.schema_editor() as schema_editor:
        backfill_live_state(global_apps, schema_editor)


def make_legacy_usecase(*, slug, title, persona, languages=("en",), status=None,
                        with_marker=False, record_publication=True,
                        strip_persona_from_snapshot=True):
    """
    Build a use case in the shape a pre-Beta-11.7 record has: a live snapshot
    without a ``persona`` key, optionally without ``last_published_revision_id``,
    and with a real reversion history.
    """
    status = status or EditorialWorkflowMixin.STATUS_PUBLISHED
    usecase = UseCase.objects.create(status=EditorialWorkflowMixin.STATUS_DRAFT)
    for language in languages:
        usecase.create_translation(
            language,
            title=f"{title} {language}",
            intro="i", body="b", outro="o",
            slug=f"{slug}-{language}",
            persona=f"{persona} {language}",
        )

    # Real FSM path to published, so live_i18n is written by the real
    # _update_live_snapshot() rather than assembled by hand.
    usecase.move_to_review(by=None)
    usecase.save()
    usecase.approve(by=None)
    usecase.save()
    usecase.publish(by=None)
    usecase.published_at = usecase.published_at or timezone.now()
    usecase.save()

    if strip_persona_from_snapshot:
        # Pre-Beta-11.7 shape: LIVE_SNAPSHOT_FIELDS had no "persona", so the
        # key was absent from live_i18n *and* from every revision recording
        # it. Stripping before the publication revision is what makes this
        # fixture a faithful legacy record rather than an impossible hybrid.
        legacy = copy.deepcopy(usecase.live_i18n)
        for values in legacy.values():
            if isinstance(values, dict):
                values.pop("persona", None)
        UseCase.objects.filter(pk=usecase.pk).update(live_i18n=legacy)

    if record_publication:
        # The publishing revision: recorded with the snapshot in place, so
        # its serialized live_i18n equals the snapshot now in the database
        # and its translation rows are the published ones.
        with reversion.create_revision():
            reversion.set_comment("Admin-Action: publish")
            fresh = UseCase.objects.get(pk=usecase.pk)
            fresh.save()
            for tr in UseCaseTranslation.objects.filter(master_id=fresh.pk):
                tr.save()

    if with_marker:
        marker = (
            Version.objects.filter(
                content_type=ContentType.objects.get_for_model(UseCase),
                object_id=str(usecase.pk),
            )
            .order_by("-pk")
            .first()
        )
        UseCase.objects.filter(pk=usecase.pk).update(
            last_published_revision_id=marker.pk if marker else 1
        )
    else:
        UseCase.objects.filter(pk=usecase.pk).update(last_published_revision_id=None)

    if status != EditorialWorkflowMixin.STATUS_PUBLISHED:
        UseCase.objects.filter(pk=usecase.pk).update(status=status)

    return UseCase.objects.get(pk=usecase.pk)


class BackfillBaseTestCase(TestCase):
    def setUp(self):
        translation.activate(settings.LANGUAGE_CODE)
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)

    def usecase_versions(self, usecase):
        ct = ContentType.objects.get_for_model(UseCase)
        return Version.objects.filter(content_type=ct, object_id=str(usecase.pk)).order_by("-pk")


class MissingRevisionMarkerTests(BackfillBaseTestCase):
    """Group A: a live snapshot whose marker was never written."""

    def test_marker_is_set_to_the_publication_version(self):
        usecase = make_legacy_usecase(
            slug="legacy-marker", title="Legacy Marker", persona="Legacy Persona",
            status=EditorialWorkflowMixin.STATUS_PUBLISHED,
        )
        self.assertIsNone(usecase.last_published_revision_id)

        run_backfill()

        refreshed = UseCase.objects.get(pk=usecase.pk)
        self.assertIsNotNone(refreshed.last_published_revision_id)

        marker_version = Version.objects.get(pk=refreshed.last_published_revision_id)
        fields = json.loads(marker_version.serialized_data)[0]["fields"]
        stored = fields["live_i18n"]
        stored = json.loads(stored) if isinstance(stored, str) else stored
        # The marker points at a version that is genuinely a publication of
        # this very snapshot - both signals the algorithm requires.
        self.assertTrue(fields["is_published"])
        self.assertEqual(
            {lang: {k: v for k, v in vals.items() if k != "persona"}
             for lang, vals in stored.items()},
            {lang: {k: v for k, v in vals.items() if k != "persona"}
             for lang, vals in refreshed.live_i18n.items()},
        )

    def test_public_page_survives_the_backfill_in_an_editing_state(self):
        usecase = make_legacy_usecase(
            slug="legacy-visible", title="Legacy Visible", persona="Legacy Persona",
            status=EditorialWorkflowMixin.STATUS_REVIEW,
        )
        self.assertFalse(
            UseCase.objects.visible_in_language("en").filter(pk=usecase.pk).exists()
        )

        run_backfill()

        self.assertTrue(
            UseCase.objects.visible_in_language("en").filter(pk=usecase.pk).exists()
        )
        self.assertEqual(self.client.get("/en/usecases/legacy-visible-en/").status_code, 200)

    def test_no_new_revision_is_created(self):
        make_legacy_usecase(
            slug="legacy-norev", title="Legacy NoRev", persona="Legacy Persona",
            status=EditorialWorkflowMixin.STATUS_PUBLISHED,
        )
        before = Version.objects.count()
        run_backfill()
        self.assertEqual(Version.objects.count(), before)

    def test_archived_record_without_a_provable_version_does_not_block(self):
        """Archiving is the deliberate withdrawal, so the marker can never
        make such a record public - demanding one would block deploys."""
        usecase = make_legacy_usecase(
            slug="legacy-archived", title="Legacy Archived", persona="Legacy Persona",
            status=EditorialWorkflowMixin.STATUS_ARCHIVED, record_publication=False,
        )
        self.assertIsNone(usecase.last_published_revision_id)

        run_backfill()

        refreshed = UseCase.objects.get(pk=usecase.pk)
        self.assertIsNone(refreshed.last_published_revision_id)
        self.assertFalse(
            UseCase.objects.visible_in_language("en").filter(pk=usecase.pk).exists()
        )


class WrongRevisionCandidateTests(BackfillBaseTestCase):
    """Group B: newer draft revisions must never win."""

    def test_newer_draft_revision_is_not_chosen(self):
        usecase = make_legacy_usecase(
            slug="legacy-candidates", title="Legacy Candidates", persona="Legacy Persona",
            status=EditorialWorkflowMixin.STATUS_PUBLISHED,
        )
        publication_pk = self.usecase_versions(usecase).first().pk

        # A later editing round: new draft values, recorded as a revision,
        # and the object leaves the published state.
        fresh = UseCase.objects.get(pk=usecase.pk)
        fresh.set_current_language("en")
        fresh.title = "Legacy Candidates Draft"
        fresh.persona = "Draft Persona Never Published"
        fresh.save()
        fresh.move_to_review(by=None)
        fresh.save()
        with reversion.create_revision():
            reversion.set_comment("draft edit")
            again = UseCase.objects.get(pk=usecase.pk)
            again.save()
            for tr in UseCaseTranslation.objects.filter(master_id=again.pk):
                tr.save()

        newest_pk = self.usecase_versions(usecase).first().pk
        self.assertGreater(newest_pk, publication_pk, "a newer revision exists")

        run_backfill()

        refreshed = UseCase.objects.get(pk=usecase.pk)
        self.assertEqual(refreshed.last_published_revision_id, publication_pk)
        self.assertNotEqual(refreshed.last_published_revision_id, newest_pk)

    def test_draft_persona_is_not_copied_into_the_snapshot(self):
        usecase = make_legacy_usecase(
            slug="legacy-draftpersona", title="Legacy DraftPersona",
            persona="Published Persona", status=EditorialWorkflowMixin.STATUS_PUBLISHED,
        )
        fresh = UseCase.objects.get(pk=usecase.pk)
        fresh.set_current_language("en")
        fresh.persona = "Draft Persona Never Published"
        fresh.save()

        run_backfill()

        refreshed = UseCase.objects.get(pk=usecase.pk)
        self.assertEqual(refreshed.live_i18n["en"]["persona"], "Published Persona en")


class UnresolvableStateFailsClosedTests(BackfillBaseTestCase):
    """Group C: no provable publication version, and it matters."""

    def test_backfill_aborts_and_changes_nothing(self):
        usecase = make_legacy_usecase(
            slug="legacy-unresolvable", title="Legacy Unresolvable",
            persona="Legacy Persona", status=EditorialWorkflowMixin.STATUS_PUBLISHED,
            record_publication=False,
        )
        self.assertIsNone(usecase.last_published_revision_id)

        with self.assertRaises(RuntimeError) as ctx:
            run_backfill()

        message = str(ctx.exception)
        self.assertIn(str(usecase.pk), message)
        # Only technical identifiers - never content.
        self.assertNotIn("Legacy Unresolvable", message)
        self.assertNotIn("Legacy Persona", message)

        refreshed = UseCase.objects.get(pk=usecase.pk)
        self.assertIsNone(refreshed.last_published_revision_id)


class PersonaBackfillTests(BackfillBaseTestCase):
    """Groups D/E/F: where the published persona comes from."""

    def test_existing_snapshot_persona_is_never_overwritten(self):
        usecase = make_legacy_usecase(
            slug="legacy-personakeep", title="Legacy PersonaKeep",
            persona="Original Persona", status=EditorialWorkflowMixin.STATUS_PUBLISHED,
            strip_persona_from_snapshot=False,
        )
        before = copy.deepcopy(usecase.live_i18n)
        self.assertIn("persona", before["en"])

        fresh = UseCase.objects.get(pk=usecase.pk)
        fresh.set_current_language("en")
        fresh.persona = "Draft Persona"
        fresh.save()

        run_backfill()

        refreshed = UseCase.objects.get(pk=usecase.pk)
        self.assertEqual(refreshed.live_i18n["en"]["persona"], before["en"]["persona"])

    def test_persona_is_reconstructed_from_the_publication_revision(self):
        usecase = make_legacy_usecase(
            slug="legacy-personarev", title="Legacy PersonaRev",
            persona="Published Persona", status=EditorialWorkflowMixin.STATUS_PUBLISHED,
        )
        self.assertNotIn("persona", usecase.live_i18n["en"])

        run_backfill()

        refreshed = UseCase.objects.get(pk=usecase.pk)
        self.assertEqual(refreshed.live_i18n["en"]["persona"], "Published Persona en")

    def test_compatibility_fallback_uses_the_current_same_language_persona(self):
        """No publication revision for the persona, but the record already
        has a marker - so only persona needs filling. Pre-11.7 the public
        card rendered exactly this current value."""
        usecase = make_legacy_usecase(
            slug="legacy-personafallback", title="Legacy PersonaFallback",
            persona="Current Persona", status=EditorialWorkflowMixin.STATUS_PUBLISHED,
            record_publication=False, with_marker=True,
        )
        self.assertIsNotNone(usecase.last_published_revision_id)
        self.assertNotIn("persona", usecase.live_i18n["en"])

        run_backfill()

        refreshed = UseCase.objects.get(pk=usecase.pk)
        self.assertEqual(refreshed.live_i18n["en"]["persona"], "Current Persona en")

    def test_only_persona_is_taken_from_the_current_translation(self):
        usecase = make_legacy_usecase(
            slug="legacy-onlypersona", title="Legacy OnlyPersona",
            persona="Current Persona", status=EditorialWorkflowMixin.STATUS_PUBLISHED,
            record_publication=False, with_marker=True,
        )
        published_title = usecase.live_i18n["en"]["title"]

        fresh = UseCase.objects.get(pk=usecase.pk)
        fresh.set_current_language("en")
        fresh.title = "Draft Title Never Published"
        fresh.intro = "Draft intro never published"
        fresh.save()

        run_backfill()

        refreshed = UseCase.objects.get(pk=usecase.pk)
        self.assertEqual(refreshed.live_i18n["en"]["title"], published_title)
        self.assertNotIn("Draft Title Never Published", json.dumps(refreshed.live_i18n))
        self.assertNotIn("Draft intro never published", json.dumps(refreshed.live_i18n))


class MultiLanguageBackfillTests(BackfillBaseTestCase):
    """Group G: per-language, never across languages."""

    def test_each_language_gets_its_own_persona(self):
        usecase = make_legacy_usecase(
            slug="legacy-multilang", title="Legacy MultiLang",
            persona="Persona", languages=("en", "de"),
            status=EditorialWorkflowMixin.STATUS_PUBLISHED,
        )
        run_backfill()

        refreshed = UseCase.objects.get(pk=usecase.pk)
        self.assertEqual(refreshed.live_i18n["en"]["persona"], "Persona en")
        self.assertEqual(refreshed.live_i18n["de"]["persona"], "Persona de")

    def test_no_language_is_added_to_the_snapshot(self):
        usecase = make_legacy_usecase(
            slug="legacy-nolangadd", title="Legacy NoLangAdd", persona="Persona",
            languages=("en",), status=EditorialWorkflowMixin.STATUS_PUBLISHED,
        )
        usecase.create_translation(
            "de", title="Nur Entwurf", intro="i", body="b", outro="o",
            slug="legacy-nolangadd-de-draft", persona="Entwurf Persona",
        )

        run_backfill()

        refreshed = UseCase.objects.get(pk=usecase.pk)
        self.assertEqual(sorted(refreshed.live_i18n.keys()), ["en"])
        self.assertNotIn("Entwurf Persona", json.dumps(refreshed.live_i18n))


class DataIntegrityTests(BackfillBaseTestCase):
    """Group H: nothing but the two owned fields may change."""

    def _state(self, pk):
        usecase = UseCase.objects.get(pk=pk)
        translations = list(
            UseCaseTranslation.objects.filter(master_id=pk)
            .order_by("language_code")
            .values("language_code", "title", "intro", "body", "outro", "slug",
                    "public_slug", "persona")
        )
        return {
            "status": usecase.status,
            "reviewed_at": usecase.reviewed_at,
            "reviewed_by_id": usecase.reviewed_by_id,
            "published_at": usecase.published_at,
            "updated_at": usecase.updated_at,
            "is_published": usecase.is_published,
            "tools": list(usecase.tools.values_list("pk", flat=True)),
            "translations": translations,
            "revisions": Version.objects.count(),
        }

    def test_only_marker_and_snapshot_persona_change(self):
        usecase = make_legacy_usecase(
            slug="legacy-integrity", title="Legacy Integrity", persona="Persona",
            languages=("en", "de"), status=EditorialWorkflowMixin.STATUS_PUBLISHED,
        )
        before = self._state(usecase.pk)
        snapshot_before = copy.deepcopy(UseCase.objects.get(pk=usecase.pk).live_i18n)

        run_backfill()

        self.assertEqual(before, self._state(usecase.pk))

        after = UseCase.objects.get(pk=usecase.pk).live_i18n
        for language, values in snapshot_before.items():
            for key, value in values.items():
                with self.subTest(language=language, key=key):
                    self.assertEqual(after[language][key], value)
            self.assertEqual(
                set(after[language]) - set(values), {"persona"}
            )


class IdempotencyTests(BackfillBaseTestCase):
    """Group I: a second run is a no-op."""

    def test_second_run_changes_nothing(self):
        usecase = make_legacy_usecase(
            slug="legacy-idempotent", title="Legacy Idempotent", persona="Persona",
            languages=("en", "de"), status=EditorialWorkflowMixin.STATUS_PUBLISHED,
        )
        run_backfill()

        first = UseCase.objects.get(pk=usecase.pk)
        first_snapshot = copy.deepcopy(first.live_i18n)
        first_marker = first.last_published_revision_id
        first_updated = first.updated_at
        revisions = Version.objects.count()

        run_backfill()

        second = UseCase.objects.get(pk=usecase.pk)
        self.assertEqual(second.live_i18n, first_snapshot)
        self.assertEqual(second.last_published_revision_id, first_marker)
        self.assertEqual(second.updated_at, first_updated)
        self.assertEqual(Version.objects.count(), revisions)


class DeploymentInvariantTests(BackfillBaseTestCase):
    """Beta 11.7A deployment invariant, asserted over the whole table."""

    def test_after_backfill_no_live_snapshot_lacks_marker_or_persona(self):
        make_legacy_usecase(
            slug="inv-a", title="Invariant A", persona="Persona",
            languages=("en", "de"), status=EditorialWorkflowMixin.STATUS_PUBLISHED,
        )
        make_legacy_usecase(
            slug="inv-b", title="Invariant B", persona="Persona",
            status=EditorialWorkflowMixin.STATUS_REVIEW,
        )
        make_legacy_usecase(
            slug="inv-c", title="Invariant C", persona="Persona",
            status=EditorialWorkflowMixin.STATUS_ARCHIVED, record_publication=False,
        )

        run_backfill()

        for usecase in UseCase.objects.exclude(live_i18n={}).exclude(live_i18n__isnull=True):
            with self.subTest(pk=usecase.pk):
                if usecase.status != EditorialWorkflowMixin.STATUS_ARCHIVED:
                    self.assertIsNotNone(usecase.last_published_revision_id)
                for language, values in usecase.live_i18n.items():
                    self.assertIn("persona", values, f"{usecase.pk}/{language}")
