"""
Beta 11.11B2A: the schema is in place and nothing uses it yet.

That "nothing" is the point of this module. B2A adds three columns for a
review-binding mechanism whose logic - the submit-time revision, the payload
fingerprint, the approval binding, the publish guard, the invalidation - all
arrives in later slices. Until then every existing workflow path has to behave
exactly as it did before, and the columns have to stay empty. A column that
starts collecting half-meaningful values before the rule that interprets them
exists is worse than no column at all.

The module also covers the visibility consequence of the migration's status
cleanup, because that is where B2A could actually take content offline:
downgraded rows must keep serving their published snapshot, and rows that were
never provably published must stay invisible.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from reversion.models import Revision

from catalog.models import Tool
from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin as Workflow
from guides.models import Guide
from prompts.models import Prompt
from usecases.models import UseCase

User = get_user_model()

BINDING_FIELDS = ("review_revision_id", "approved_revision_id")


def refetch(obj):
    """django-fsm's protected ``status`` descriptor rejects the ``setattr``
    that ``refresh_from_db()`` performs, so reload through the manager."""
    return type(obj).objects.get(pk=obj.pk)


class ReviewBindingRuntimeTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser(
            "b2a-admin", "b2a-admin@example.com", "pw"
        )
        cls.author = User.objects.create_user(
            "b2a-author", "b2a-author@example.com", "pw", is_staff=True
        )
        Group.objects.get_or_create(name="Author")[0].user_set.add(cls.author)
        cls.tool = Tool.objects.create(slug="b2a-tool")
        cls.tool.create_translation("en", name="B2A Tool")

    def setUp(self):
        self.client.force_login(self.admin)

    def run_action(self, app_label, model_name, action, pk):
        return self.client.post(
            reverse(f"admin:{app_label}_{model_name}_changelist"),
            data={"action": action, "_selected_action": [str(pk)], "index": "0"},
            follow=True,
        )

    def assert_unbound(self, obj):
        reloaded = refetch(obj)
        for name in BINDING_FIELDS:
            self.assertIsNone(
                getattr(reloaded, name),
                f"{type(obj).__name__}.{name} was written - B2A must not activate "
                "the binding",
            )
        self.assertEqual(reloaded.review_payload_fingerprint, "")
        return reloaded


# ======================================================================
# Runtime non-activation
# ======================================================================


class WorkflowLeavesTheBindingEmptyTests(ReviewBindingRuntimeTestCase):
    """
    Admin workflow actions on a type whose admin submit path is still the
    shared, unbound one - Comparison, driven through the real admin actions.

    Beta 11.11C2B activated the *prompt* admin submit (it now binds a review
    revision and a fingerprint - covered in
    ``prompts/tests/test_admin_review_submission.py`` and asserted here by
    ``test_prompt_admin_submit_now_binds``). Guide/UseCase/Comparison keep the
    shared editorial submit action, which still writes no binding, so
    Comparison stands in for that unchanged contract in the iterating tests
    below. Prompt-specific assertions were previously part of ``_targets()``;
    they were split out when C2B changed only Prompt's behaviour.
    """

    def setUp(self):
        super().setUp()
        self.prompt = Prompt.objects.create(author=self.author)
        self.prompt.create_translation(
            "en", title="B2A Prompt", intro="i", body="b", outro="o", slug="b2a-prompt"
        )
        self.comparison = Comparison.objects.create(author=self.author)
        self.comparison.create_translation(
            "en", title="B2A Comparison", intro="i", body="b", slug="b2a-comparison"
        )

    def _targets(self):
        # Comparison only: its admin submit path is unchanged by C2B and still
        # writes no binding. Prompt's now-bound submit is asserted separately.
        return (
            ("compare", "comparison", self.comparison),
        )

    def test_submit_reaches_review_and_writes_no_binding(self):
        for app_label, model_name, obj in self._targets():
            with self.subTest(model=f"{app_label}.{model_name}"):
                self.run_action(app_label, model_name, "action_submit_for_review", obj.pk)
                reloaded = self.assert_unbound(obj)
                self.assertEqual(reloaded.status, Workflow.STATUS_REVIEW)
                self.assertIsNotNone(reloaded.submitted_for_review_at)

    def test_prompt_admin_submit_now_binds(self):
        """
        C2B contract (replacing the old 'prompt submit writes no binding'
        assertion): the prompt admin submit action now binds a review revision
        and the canonical fingerprint, while approve/publish remain
        un-activated - ``approved_revision`` stays empty and no reviewer
        metadata is written.
        """
        from core.review_binding import fingerprint_review_payload
        from prompts.review_payload import build_prompt_review_payload

        expected_fp = fingerprint_review_payload(build_prompt_review_payload(self.prompt))
        self.run_action("prompts", "prompt", "action_submit_for_review", self.prompt.pk)

        reloaded = refetch(self.prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_REVIEW)
        self.assertIsNotNone(reloaded.review_revision_id)
        self.assertEqual(reloaded.review_payload_fingerprint, expected_fp)
        self.assertIsNone(reloaded.approved_revision_id)
        self.assertIsNone(reloaded.reviewed_by_id)
        self.assertIsNone(reloaded.reviewed_at)

    def test_approve_reaches_approved_and_writes_no_binding(self):
        for app_label, model_name, obj in self._targets():
            with self.subTest(model=f"{app_label}.{model_name}"):
                self.run_action(app_label, model_name, "action_submit_for_review", obj.pk)
                self.run_action(app_label, model_name, "action_approve", obj.pk)
                reloaded = self.assert_unbound(obj)
                self.assertEqual(reloaded.status, Workflow.STATUS_APPROVED)

    def test_prompt_admin_approve_now_binds(self):
        """
        C3B contract (replacing the old 'prompt approve writes no binding'
        assertion): the prompt admin approve action now binds
        ``approved_revision`` to the exact ``review_revision`` C2B's submit
        already captured - never a fresh lookup, never a new snapshot -
        while publish remains un-activated.
        """
        self.run_action("prompts", "prompt", "action_submit_for_review", self.prompt.pk)
        submitted = refetch(self.prompt)
        review_revision_id = submitted.review_revision_id
        fingerprint = submitted.review_payload_fingerprint
        self.assertIsNotNone(review_revision_id)

        self.run_action("prompts", "prompt", "action_approve", self.prompt.pk)

        reloaded = refetch(self.prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_APPROVED)
        self.assertEqual(reloaded.review_revision_id, review_revision_id)
        self.assertEqual(reloaded.approved_revision_id, review_revision_id)
        self.assertEqual(reloaded.review_payload_fingerprint, fingerprint)
        self.assertIsNotNone(reloaded.reviewed_by_id)
        self.assertIsNotNone(reloaded.reviewed_at)

    def test_publish_reaches_published_and_writes_no_binding(self):
        for app_label, model_name, obj in self._targets():
            with self.subTest(model=f"{app_label}.{model_name}"):
                for action in (
                    "action_submit_for_review",
                    "action_approve",
                    "action_publish",
                ):
                    self.run_action(app_label, model_name, action, obj.pk)
                reloaded = self.assert_unbound(obj)
                self.assertEqual(reloaded.status, Workflow.STATUS_PUBLISHED)
                self.assertTrue(reloaded.is_published)

    def test_publish_still_writes_the_live_snapshot_unchanged(self):
        for action in ("action_submit_for_review", "action_approve", "action_publish"):
            self.run_action("prompts", "prompt", action, self.prompt.pk)
        published = refetch(self.prompt)
        self.assertEqual(sorted(published.live_i18n), ["en"])
        self.assertEqual(published.live_i18n["en"]["title"], "B2A Prompt")

    def test_comparison_publish_still_writes_the_entry_snapshot(self):
        for action in ("action_submit_for_review", "action_approve", "action_publish"):
            self.run_action("compare", "comparison", action, self.comparison.pk)
        published = refetch(self.comparison)
        self.assertEqual(published.live_entries, [])
        self.assertEqual(sorted(published.live_i18n), ["en"])

    def test_legacy_publish_marker_keeps_its_version_id_semantics(self):
        """
        Unchanged from before B2A: despite the name it stores a
        ``Version.id``, and the new FKs did not take it over.

        C2B update: the prompt submit now binds ``review_revision``, so it is
        no longer ``None`` after the workflow.

        C3B update: the prompt approve now binds ``approved_revision`` too
        (equal to ``review_revision``, never the legacy marker's value) - what
        this test still guards is that the legacy ``last_published_revision_id``
        marker keeps meaning "a ``Version.id``", distinct from either FK.
        """
        from reversion.models import Version

        for action in ("action_submit_for_review", "action_approve", "action_publish"):
            self.run_action("prompts", "prompt", action, self.prompt.pk)

        published = refetch(self.prompt)
        marker = published.last_published_revision_id
        self.assertIsNotNone(marker)
        version = Version.objects.get(id=marker)
        self.assertEqual(version.content_type.model, "prompt")
        # review_revision and approved_revision are now bound by the C2B/C3B
        # admin submit/approve; the legacy Version.id marker is a different
        # value from either Revision FK.
        self.assertIsNotNone(published.review_revision_id)
        self.assertNotEqual(published.review_revision_id, marker)
        self.assertEqual(published.approved_revision_id, published.review_revision_id)
        self.assertNotEqual(published.approved_revision_id, marker)

    def test_rework_behaves_exactly_as_before_and_writes_no_binding(self):
        for app_label, model_name, obj in self._targets():
            with self.subTest(model=f"{app_label}.{model_name}"):
                self.run_action(app_label, model_name, "action_submit_for_review", obj.pk)
                self.run_action(app_label, model_name, "action_request_rework", obj.pk)
                reloaded = self.assert_unbound(obj)
                self.assertEqual(reloaded.status, Workflow.STATUS_REWORK)

    def test_editing_during_review_now_auto_invalidates(self):
        """
        Beta 11.11C4H closed the Beta 11.11A stale gap this test used to
        document: editing a prompt's reviewed content through the real
        PromptAdmin changeform now *does* automatically invalidate the review
        binding, via the Beta 11.11C4G guard integrated into
        ``PromptAdmin.save_model()``/``save_related()``.

        Previous contract (through Beta 11.11C4G): a content edit during
        review left ``review_revision``/``review_payload_fingerprint``
        untouched - "no auto-invalidation is wired". New contract (Beta
        11.11C4H): a *real* payload change (this test changes ``title``, a
        canonical v2 payload field) fails closed - the binding is cleared and
        the prompt drops back to an unreviewed state, exactly like any other
        Beta 11.11B2B2 invalidation. Remaining protection: this is not a
        blanket "every edit invalidates" rule - an edit that leaves the v2
        payload unchanged still leaves the binding untouched (see
        ``prompts/tests/test_admin_review_edit_guard.py``).

        ``self.prompt`` (from ``setUp``) has no ``live_i18n`` yet (never
        published), so B2B2's own live-snapshot rule sends it to ``draft``,
        not ``rework``.
        """
        self.run_action("prompts", "prompt", "action_submit_for_review", self.prompt.pk)
        bound = refetch(self.prompt)
        self.assertEqual(bound.status, Workflow.STATUS_REVIEW)
        self.assertIsNotNone(bound.review_revision_id)

        self.client.post(
            reverse("admin:prompts_prompt_change", args=[self.prompt.pk]),
            data={
                "author": str(self.author.pk),
                "review_note": "",
                "published_at_0": "",
                "published_at_1": "",
                "tools": [str(self.tool.pk)],
                "slug": "b2a-prompt",
                "title": "Edited during review",
                "intro": "i",
                "body": "b",
                "outro": "o",
                "_continue": "Save",
            },
        )

        after_edit = refetch(self.prompt)
        self.assertEqual(after_edit.status, Workflow.STATUS_DRAFT)
        self.assertIsNone(after_edit.review_revision_id)
        self.assertEqual(after_edit.review_payload_fingerprint, "")
        self.assertIsNone(after_edit.approved_revision_id)


# ======================================================================
# Visibility after the migration's status cleanup
# ======================================================================


class DowngradedVisibilityTests(ReviewBindingRuntimeTestCase):
    """
    The two outcomes of the cleanup, checked against the real public surfaces
    rather than against the queryset alone.

    Rows are built in their *post-migration* shape (the migration itself is
    covered by the executor tests); what matters here is that the resulting
    states behave correctly.
    """

    SNAPSHOT_EN = {
        "en": {
            "title": "Published title",
            "slug": "b2a-live-slug",
            "public_slug": "b2a-live-slug",
            "intro": "published intro",
            "body": "published body",
        }
    }

    def _make(self, model, *, status, snapshot, marker, slug, extra=None):
        obj = model.objects.create(author=self.author)
        obj.create_translation(
            "en",
            title="Current draft title",
            intro="draft intro",
            body="draft body",
            slug=slug,
            **(extra or {}),
        )
        model.objects.filter(pk=obj.pk).update(
            status=status,
            live_i18n=snapshot,
            last_published_revision_id=marker,
        )
        return refetch(obj)

    # -- never published -> draft -> invisible ---------------------------

    def test_never_published_downgrade_is_invisible_everywhere(self):
        cases = (
            (Guide, "guides", {}),
            (Prompt, "prompts", {"outro": "o"}),
            (UseCase, "usecases", {"outro": "o", "persona": "p"}),
            (Comparison, "compare", {}),
        )
        for model, app_label, extra in cases:
            with self.subTest(model=model._meta.label):
                obj = self._make(
                    model,
                    status=Workflow.STATUS_DRAFT,
                    snapshot={},
                    marker=None,
                    slug=f"b2a-never-{app_label}",
                    extra=extra,
                )
                self.assertFalse(
                    model.objects.visible_on_site().filter(pk=obj.pk).exists()
                )
                self.assertFalse(
                    model.objects.visible_in_language("en").filter(pk=obj.pk).exists()
                )
                response = self.client.get(
                    reverse(f"{app_label}:detail", args=[f"b2a-never-{app_label}"])
                )
                self.assertEqual(response.status_code, 404)

    def test_a_draft_with_only_a_legacy_marker_stays_invisible(self):
        """
        The fail-closed case: the marker alone used to keep such a row in
        ``visible_on_site()`` while ``review``, and its public page rendered
        the *current draft* because ``live_i18n`` was empty. After the cleanup
        it is ``draft`` and gone from every surface.
        """
        for model, app_label in (
            (Guide, "guides"),
            (Prompt, "prompts"),
            (UseCase, "usecases"),
            (Comparison, "compare"),
        ):
            with self.subTest(model=model._meta.label):
                obj = self._make(
                    model,
                    status=Workflow.STATUS_DRAFT,
                    snapshot={},
                    marker=555,
                    slug=f"b2a-marker-{app_label}",
                    extra={"outro": "o", "persona": "p"} if model is UseCase
                    else ({"outro": "o"} if model is Prompt else {}),
                )
                self.assertFalse(
                    model.objects.visible_on_site().filter(pk=obj.pk).exists()
                )

    # -- previously live -> rework -> still visible -----------------------

    def test_previously_published_downgrade_stays_visible_on_all_four_types(self):
        """
        The reason B2A widened ``EditorialQuerySet.LIVE_EDITING_STATUSES``.
        Without ``rework`` in that tuple, guides and prompts would have gone
        offline here while use cases and comparisons stayed up.
        """
        for model, app_label, extra, updates in (
            (Guide, "guides", {}, {}),
            (Prompt, "prompts", {"outro": "o"}, {}),
            (UseCase, "usecases", {"outro": "o", "persona": "p"}, {}),
            (Comparison, "compare", {}, {"live_entries": []}),
        ):
            with self.subTest(model=model._meta.label):
                obj = self._make(
                    model,
                    status=Workflow.STATUS_REWORK,
                    snapshot=self.SNAPSHOT_EN,
                    marker=777,
                    slug=f"b2a-live-{app_label}",
                    extra=extra,
                )
                if updates:
                    model.objects.filter(pk=obj.pk).update(**updates)

                self.assertTrue(
                    model.objects.visible_on_site().filter(pk=obj.pk).exists(),
                    f"{model._meta.label} in rework with a live snapshot went offline",
                )
                self.assertTrue(
                    model.objects.visible_in_language("en").filter(pk=obj.pk).exists()
                )

    def test_the_public_page_serves_the_snapshot_not_the_current_draft(self):
        for model, app_label, extra, updates in (
            (Guide, "guides", {}, {}),
            (Prompt, "prompts", {"outro": "o"}, {}),
            (UseCase, "usecases", {"outro": "o", "persona": "p"}, {}),
            (Comparison, "compare", {}, {"live_entries": []}),
        ):
            with self.subTest(model=model._meta.label):
                self._make(
                    model,
                    status=Workflow.STATUS_REWORK,
                    snapshot=self.SNAPSHOT_EN,
                    marker=777,
                    slug=f"b2a-served-{app_label}",
                    extra=extra,
                )
                obj = model.objects.get(
                    translations__slug=f"b2a-served-{app_label}"
                )
                if updates:
                    model.objects.filter(pk=obj.pk).update(**updates)

                response = self.client.get(
                    reverse(f"{app_label}:detail", args=["b2a-live-slug"])
                )
                self.assertEqual(response.status_code, 200)
                content = response.content.decode()
                self.assertIn("Published title", content)
                self.assertNotIn("Current draft title", content)

    def test_comparison_with_null_entries_is_not_kept_online_by_the_widening(self):
        """
        Comparison keeps its stricter override: ``live_entries IS NULL`` means
        the public page would have to read entries from the live rows, so such
        a record stays offline outside ``published`` - the ``rework`` widening
        does not change that.
        """
        obj = self._make(
            Comparison,
            status=Workflow.STATUS_REWORK,
            snapshot=self.SNAPSHOT_EN,
            marker=777,
            slug="b2a-null-entries",
        )
        Comparison.objects.filter(pk=obj.pk).update(live_entries=None)
        self.assertFalse(
            Comparison.objects.visible_on_site().filter(pk=obj.pk).exists()
        )

    def test_published_content_is_unaffected(self):
        for model, app_label, extra, updates in (
            (Guide, "guides", {}, {}),
            (Prompt, "prompts", {"outro": "o"}, {}),
            (UseCase, "usecases", {"outro": "o", "persona": "p"}, {}),
            (Comparison, "compare", {}, {"live_entries": []}),
        ):
            with self.subTest(model=model._meta.label):
                obj = self._make(
                    model,
                    status=Workflow.STATUS_PUBLISHED,
                    snapshot=self.SNAPSHOT_EN,
                    marker=888,
                    slug=f"b2a-pub-{app_label}",
                    extra=extra,
                )
                if updates:
                    model.objects.filter(pk=obj.pk).update(**updates)
                self.assertTrue(
                    model.objects.visible_on_site().filter(pk=obj.pk).exists()
                )
                self.assertEqual(refetch(obj).status, Workflow.STATUS_PUBLISHED)


# ======================================================================
# Admin and request smoke
# ======================================================================


class AdminSmokeAfterSchemaChangeTests(ReviewBindingRuntimeTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.guide = Guide.objects.create(author=cls.author)
        cls.guide.create_translation(
            "en", title="Smoke Guide", intro="i", body="b", slug="b2a-smoke-guide"
        )
        cls.prompt = Prompt.objects.create(author=cls.author)
        cls.prompt.create_translation(
            "en", title="Smoke Prompt", intro="i", body="b", outro="o",
            slug="b2a-smoke-prompt",
        )
        cls.usecase = UseCase.objects.create(author=cls.author)
        cls.usecase.create_translation(
            "en", title="Smoke UC", intro="i", body="b", outro="o", persona="p",
            slug="b2a-smoke-uc",
        )
        cls.comparison = Comparison.objects.create(author=cls.author)
        cls.comparison.create_translation(
            "en", title="Smoke CMP", intro="i", body="b", slug="b2a-smoke-cmp"
        )

    def _objects(self):
        return (
            ("guides", "guide", self.guide),
            ("prompts", "prompt", self.prompt),
            ("usecases", "usecase", self.usecase),
            ("compare", "comparison", self.comparison),
        )

    def test_changelists_and_change_forms_still_render(self):
        for app_label, model_name, obj in self._objects():
            with self.subTest(model=f"{app_label}.{model_name}"):
                self.assertEqual(
                    self.client.get(
                        reverse(f"admin:{app_label}_{model_name}_changelist")
                    ).status_code,
                    200,
                )
                self.assertEqual(
                    self.client.get(
                        reverse(f"admin:{app_label}_{model_name}_change", args=[obj.pk])
                    ).status_code,
                    200,
                )

    def test_the_binding_fields_are_not_rendered_in_the_change_form(self):
        for app_label, model_name, obj in self._objects():
            response = self.client.get(
                reverse(f"admin:{app_label}_{model_name}_change", args=[obj.pk])
            )
            content = response.content.decode()
            with self.subTest(model=f"{app_label}.{model_name}"):
                for name in (
                    "review_revision",
                    "approved_revision",
                    "review_payload_fingerprint",
                ):
                    self.assertNotIn(f'name="{name}"', content)

    def test_version_history_is_still_reachable(self):
        for app_label, model_name, obj in self._objects():
            with self.subTest(model=f"{app_label}.{model_name}"):
                self.assertEqual(
                    self.client.get(
                        reverse(f"admin:{app_label}_{model_name}_history", args=[obj.pk])
                    ).status_code,
                    200,
                )

    def test_the_draft_preview_button_context_is_unchanged(self):
        for app_label, model_name, obj in self._objects():
            with self.subTest(model=f"{app_label}.{model_name}"):
                context = self.client.get(
                    reverse(f"admin:{app_label}_{model_name}_change", args=[obj.pk])
                ).context
                self.assertTrue(context["show_draft_preview"])
                self.assertEqual(context["draft_preview_language"], "en")

    def test_a_plain_get_creates_no_revision(self):
        before = Revision.objects.count()
        for app_label, model_name, obj in self._objects():
            self.client.get(reverse(f"admin:{app_label}_{model_name}_changelist"))
            self.client.get(
                reverse(f"admin:{app_label}_{model_name}_change", args=[obj.pk])
            )
        self.assertEqual(Revision.objects.count(), before)
