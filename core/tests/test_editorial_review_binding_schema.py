"""
Beta 11.11B2A: the review-binding schema contract on all four editorial types.

The three columns added here are internal workflow metadata for a mechanism
that does not exist yet. That makes two properties worth pinning right now,
before any slice starts writing to them:

1. the *shape* is what later slices will rely on - in particular that both FKs
   point at ``reversion.Revision`` (the whole graph) and not at
   ``reversion.Version`` (a single row), which is exactly the confusion the
   legacy ``last_published_revision_id`` marker embodies; and
2. the columns stay invisible and unwritable through every ordinary path -
   admin forms, Parler language tabs, inlines - so nothing can populate them
   by accident before the binding logic is designed.

The migration behaviour lives in
``core/tests/test_editorial_review_binding_migration.py``; the runtime
non-activation in ``core/tests/test_editorial_review_binding_runtime.py``.
"""
from django.contrib import admin as django_admin
from django.contrib.auth import get_user_model
from django.db import models
from django.test import RequestFactory, TestCase
from django.utils import timezone
from reversion.models import Revision, Version

from compare.models import Comparison
from core.models.editorial import EditorialQuerySet, EditorialWorkflowMixin
from guides.models import Guide
from prompts.models import Prompt
from usecases.models import UseCase

User = get_user_model()

#: (label, model, admin class) for every concrete editorial type.
EDITORIAL_TYPES = (
    ("guides.Guide", Guide),
    ("prompts.Prompt", Prompt),
    ("usecases.UseCase", UseCase),
    ("compare.Comparison", Comparison),
)

BINDING_FK_FIELDS = ("review_revision", "approved_revision")


class ReviewBindingFieldContractTests(TestCase):
    def test_every_editorial_type_has_all_three_binding_fields(self):
        for label, model in EDITORIAL_TYPES:
            for name in BINDING_FK_FIELDS + ("review_payload_fingerprint",):
                with self.subTest(model=label, field=name):
                    self.assertIsNotNone(model._meta.get_field(name))

    def test_both_foreign_keys_target_revision_not_version(self):
        """
        The distinction this whole slice rests on. ``Version`` is one
        serialized row; ``Revision`` is the transaction that groups the parent,
        its children and every translation - the unit Beta 11.11B1's manifest
        now records and the only one a review can meaningfully be bound to.
        """
        for label, model in EDITORIAL_TYPES:
            for name in BINDING_FK_FIELDS:
                with self.subTest(model=label, field=name):
                    field = model._meta.get_field(name)
                    self.assertIsInstance(field, models.ForeignKey)
                    self.assertIs(field.remote_field.model, Revision)
                    self.assertIsNot(field.remote_field.model, Version)

    def test_both_foreign_keys_are_nullable_blank_and_not_editable(self):
        for label, model in EDITORIAL_TYPES:
            for name in BINDING_FK_FIELDS:
                with self.subTest(model=label, field=name):
                    field = model._meta.get_field(name)
                    self.assertTrue(field.null)
                    self.assertTrue(field.blank)
                    self.assertFalse(field.editable)

    def test_both_foreign_keys_use_set_null(self):
        """
        Reversion housekeeping (``deleterevisions``) must never delete
        editorial content, and editorial content must never block it. CASCADE
        would do the first, PROTECT the second.
        """
        for label, model in EDITORIAL_TYPES:
            for name in BINDING_FK_FIELDS:
                with self.subTest(model=label, field=name):
                    field = model._meta.get_field(name)
                    self.assertIs(field.remote_field.on_delete, models.SET_NULL)

    def test_neither_foreign_key_creates_a_reverse_accessor_on_revision(self):
        """``related_name="+"``: four editorial types times two FKs would
        otherwise put eight accessors on a third-party model."""
        for label, model in EDITORIAL_TYPES:
            for name in BINDING_FK_FIELDS:
                with self.subTest(model=label, field=name):
                    field = model._meta.get_field(name)
                    self.assertEqual(field.remote_field.related_name, "+")
                    self.assertTrue(field.remote_field.hidden)

        accessors = [
            rel.get_accessor_name()
            for rel in Revision._meta.related_objects
            if rel.related_model in {model for _label, model in EDITORIAL_TYPES}
        ]
        self.assertEqual(accessors, [])

    def test_fingerprint_holds_exactly_a_sha256_hexdigest(self):
        for label, model in EDITORIAL_TYPES:
            with self.subTest(model=label):
                field = model._meta.get_field("review_payload_fingerprint")
                self.assertIsInstance(field, models.CharField)
                self.assertEqual(field.max_length, 64)
                self.assertTrue(field.blank)
                self.assertFalse(field.null)
                self.assertEqual(field.default, "")
                self.assertFalse(field.editable)

    def test_a_sha256_hexdigest_actually_fits(self):
        import hashlib

        digest = hashlib.sha256(b"editorial payload").hexdigest()
        self.assertEqual(len(digest), 64)
        for label, model in EDITORIAL_TYPES:
            with self.subTest(model=label):
                self.assertLessEqual(
                    len(digest), model._meta.get_field("review_payload_fingerprint").max_length
                )

    def test_legacy_publish_marker_is_untouched_and_still_not_a_foreign_key(self):
        """
        ``last_published_revision_id`` holds a ``Version.id``. B2A neither
        renames it, nor converts it to an FK, nor reinterprets it - a test
        rather than a comment, because the name invites exactly that mistake.
        """
        for label, model in EDITORIAL_TYPES:
            with self.subTest(model=label):
                field = model._meta.get_field("last_published_revision_id")
                self.assertIsInstance(field, models.IntegerField)
                self.assertNotIsInstance(field, models.ForeignKey)
                self.assertTrue(field.null)


class ReviewBindingDefaultsTests(TestCase):
    def test_a_freshly_created_object_is_unbound_on_every_type(self):
        for label, model in EDITORIAL_TYPES:
            with self.subTest(model=label):
                obj = model.objects.create()
                self.assertIsNone(obj.review_revision_id)
                self.assertIsNone(obj.approved_revision_id)
                self.assertEqual(obj.review_payload_fingerprint, "")

    def test_the_unbound_state_survives_a_reload(self):
        for label, model in EDITORIAL_TYPES:
            with self.subTest(model=label):
                pk = model.objects.create().pk
                reloaded = model.objects.get(pk=pk)
                self.assertIsNone(reloaded.review_revision_id)
                self.assertIsNone(reloaded.approved_revision_id)
                self.assertEqual(reloaded.review_payload_fingerprint, "")


class ReviewBindingSetNullTests(TestCase):
    """
    The delete contract, exercised against a real ``Revision`` row per type.

    Nothing here touches reversion's global registration or housekeeping
    settings; it deletes one ``Revision`` object and observes the effect.
    """

    @staticmethod
    def _make_revision(comment):
        """``Revision.date_created`` carries no Python-side default - reversion
        fills it when it opens a revision block - so a directly created row has
        to supply it."""
        return Revision.objects.create(date_created=timezone.now(), comment=comment)

    def test_deleting_the_bound_revision_nulls_both_fields_and_keeps_the_object(self):
        for label, model in EDITORIAL_TYPES:
            with self.subTest(model=label):
                revision = self._make_revision(f"bound {label}")
                obj = model.objects.create(
                    review_note="reviewer feedback worth keeping",
                    last_published_revision_id=4242,
                )
                model.objects.filter(pk=obj.pk).update(
                    review_revision=revision,
                    approved_revision=revision,
                    review_payload_fingerprint="a" * 64,
                )
                bound = model.objects.get(pk=obj.pk)
                self.assertEqual(bound.review_revision_id, revision.pk)
                self.assertEqual(bound.approved_revision_id, revision.pk)

                revision.delete()

                survivor = model.objects.get(pk=obj.pk)
                self.assertIsNone(survivor.review_revision_id)
                self.assertIsNone(survivor.approved_revision_id)
                # Only the binding is lost - content, status and the legacy
                # marker are untouched, and the fingerprint is deliberately
                # NOT cleared by the database: detecting "fingerprint set but
                # revision gone" is the later publish guard's job.
                self.assertEqual(survivor.status, EditorialWorkflowMixin.STATUS_DRAFT)
                self.assertEqual(survivor.review_note, "reviewer feedback worth keeping")
                self.assertEqual(survivor.last_published_revision_id, 4242)
                self.assertEqual(survivor.review_payload_fingerprint, "a" * 64)

    def test_deleting_a_revision_is_not_blocked_by_a_bound_editorial_object(self):
        revision = self._make_revision("housekeeping target")
        guide = Guide.objects.create()
        Guide.objects.filter(pk=guide.pk).update(review_revision=revision)

        revision.delete()

        self.assertFalse(Revision.objects.filter(pk=revision.pk).exists())
        self.assertTrue(Guide.objects.filter(pk=guide.pk).exists())


class ReviewBindingIsNotEditableTests(TestCase):
    """The columns must not be reachable through any ordinary editing path."""

    @classmethod
    def setUpTestData(cls):
        cls.admin_user = User.objects.create_superuser(
            "b2a-schema-admin", "b2a-schema@example.com", "pw"
        )

    def _request(self):
        request = RequestFactory().get("/admin/")
        request.user = self.admin_user
        return request

    def test_no_admin_form_exposes_the_binding_fields(self):
        for label, model in EDITORIAL_TYPES:
            model_admin = django_admin.site._registry[model]
            form = model_admin.get_form(self._request())
            with self.subTest(model=label):
                for name in BINDING_FK_FIELDS + ("review_payload_fingerprint",):
                    self.assertNotIn(name, form.base_fields)

    def test_no_admin_fieldset_mentions_the_binding_fields(self):
        for label, model in EDITORIAL_TYPES:
            model_admin = django_admin.site._registry[model]
            declared = []
            for _name, options in model_admin.get_fieldsets(self._request()):
                declared.extend(options.get("fields", ()))
            flattened = []
            for entry in declared:
                flattened.extend(entry if isinstance(entry, (list, tuple)) else [entry])
            with self.subTest(model=label):
                for name in BINDING_FK_FIELDS + ("review_payload_fingerprint",):
                    self.assertNotIn(name, flattened)

    def test_the_fields_are_not_translated_so_they_cannot_appear_in_a_language_tab(self):
        """Parler tabs render translated fields; these live on the shared
        (untranslated) row, one value per object, not one per language."""
        for label, model in EDITORIAL_TYPES:
            translated = set(model._parler_meta.get_all_fields())
            with self.subTest(model=label):
                for name in BINDING_FK_FIELDS + ("review_payload_fingerprint",):
                    self.assertNotIn(name, translated)

    def test_no_inline_formset_exposes_the_binding_fields(self):
        for label, model in EDITORIAL_TYPES:
            model_admin = django_admin.site._registry[model]
            for inline in model_admin.get_inline_instances(self._request()):
                formset = inline.get_formset(self._request())
                with self.subTest(model=label, inline=type(inline).__name__):
                    for name in BINDING_FK_FIELDS + ("review_payload_fingerprint",):
                        self.assertNotIn(name, formset.form.base_fields)

    def test_the_fields_are_not_offered_as_filters_or_search(self):
        for label, model in EDITORIAL_TYPES:
            model_admin = django_admin.site._registry[model]
            exposed = set(model_admin.list_filter) | set(model_admin.search_fields) | set(
                model_admin.list_display
            )
            with self.subTest(model=label):
                for name in BINDING_FK_FIELDS + ("review_payload_fingerprint",):
                    self.assertNotIn(name, exposed)


class LiveEditingStatusContractTests(TestCase):
    """
    ``EditorialQuerySet.LIVE_EDITING_STATUSES`` is spelled with literals
    because ``EditorialWorkflowMixin`` is defined further down the same module.
    This keeps the two from drifting apart.
    """

    def test_the_literals_match_the_status_constants(self):
        self.assertEqual(
            EditorialQuerySet.LIVE_EDITING_STATUSES,
            (
                # Beta 11.11D1 added STATUS_DRAFT: every automatic
                # invalidation now lands there, so excluding it would take
                # exactly the pages offline that B2A's rework detour existed
                # to keep online.
                EditorialWorkflowMixin.STATUS_DRAFT,
                EditorialWorkflowMixin.STATUS_REVIEW,
                EditorialWorkflowMixin.STATUS_APPROVED,
                EditorialWorkflowMixin.STATUS_REWORK,
            ),
        )

    def test_rework_is_included_so_b2a_downgrades_do_not_go_offline(self):
        """
        The reason B2A touches this at all: its cleanup moves previously
        published guides and prompts into ``rework``, and the base rule used to
        drop them from every public surface. Use cases (Beta 11.7A) and
        comparisons (Beta 11.9) had already reached the same conclusion in
        their own overrides.
        """
        self.assertIn(
            EditorialWorkflowMixin.STATUS_REWORK, EditorialQuerySet.LIVE_EDITING_STATUSES
        )

    def test_archived_stays_out(self):
        """
        Beta 11.11D1 narrowed this to ``archived`` alone - ``draft`` joined
        the set (see ``test_the_literals_match_the_status_constants``).
        Archiving is still the deliberate public withdrawal and outranks any
        snapshot on record; it is additionally excluded by the publication
        proof, because ``archive()`` clears ``is_published``.
        """
        self.assertNotIn(
            EditorialWorkflowMixin.STATUS_ARCHIVED,
            EditorialQuerySet.LIVE_EDITING_STATUSES,
        )
