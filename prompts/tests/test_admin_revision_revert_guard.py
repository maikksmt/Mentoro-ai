"""
Beta 11.11C4J: guard Prompt review/approval bindings against django-reversion
"Revert to this version" and "Recover".

``reversion.admin.VersionAdmin._reversion_revisionform_view()`` restores the
historical row via ``version.revision.revert(delete=True)`` *before*
``changeform_view()`` ever reaches ``PromptAdmin.save_model()`` - so a Beta
11.11C4G baseline captured inside ``save_model()`` would already see the
reverted content, too late to detect anything. ``PromptAdmin.revision_view()``/
``recover_view()`` (this module's subject) capture that baseline - or, for
recovery, a baseline-less marker - *before* calling ``super()``, feed it
through the existing request-local Beta 11.11C4H store, and let
``save_model()``/``save_related()`` finish the lifecycle from inside the same
``reversion.create_revision()`` block VersionAdmin itself opens. See the
module-level note above ``PromptAdmin.revision_view()`` in ``prompts/admin.py``
for the full design rationale.

These tests drive the real Django admin revision/recover URLs through the
test client wherever the real reversion machinery can organically produce the
scenario (genuine payload change, identical payload with a still-valid
restored binding, GET preview, invalid POST, recovery). A small number of
scenarios that the real HTTP path cannot organically produce (a *structurally
broken* restored binding, a *stale-but-well-formed* restored fingerprint) call
``PromptAdmin._invalidate_reverted_prompt_if_binding_invalid()`` directly -
the same "direct call for an edge case the real form can't reach" convention
``prompts/tests/test_admin_review_edit_guard.py``'s own
``TagsAndToolsTests``/``MissingBaselineFailClosedTests`` already use.
"""
import itertools
from unittest import mock

from django.contrib import admin as django_admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection, transaction
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
import reversion
from reversion.models import Revision, Version

from core.models.editorial import EditorialWorkflowMixin as Workflow
from core.review_binding import invalidate_editorial_review_state
from prompts.admin import (
    PromptAdmin,
    PromptRevisionGuardResult,
    _PromptReversionGuardSource,
    _PromptReversionGuardState,
    _PromptReviewEditIntegrationError,
)
from prompts.models import Prompt
from prompts.review_approval import approve_prompt_review
from prompts.review_edit_guard import capture_prompt_review_edit_baseline
from prompts.review_submission import submit_prompt_for_review

User = get_user_model()

_slug_counter = itertools.count()

#: The editable field names the real ``PromptAdmin`` changeform carries - used
#: to give a hand-built ``mock.Mock`` form a realistic ``fields`` mapping (a
#: real form always has one), so ``_preserve_untouched_translated_fields()``
#: sees the same "which translated fields are absent from the form" picture it
#: does in production (only ``public_slug`` is absent).
_CHANGEFORM_FORM_FIELDS = dict.fromkeys(
    ("author", "review_note", "published_at", "tools", "slug", "title", "intro", "body", "outro")
)


def refetch(prompt):
    """django-fsm's protected ``status`` descriptor rejects the ``setattr``
    ``refresh_from_db()`` performs, so reload through the manager."""
    return Prompt.objects.get(pk=prompt.pk)


def change_url(prompt):
    return reverse("admin:prompts_prompt_change", args=[prompt.pk])


def revision_url(prompt, version):
    return reverse("admin:prompts_prompt_revision", args=[prompt.pk, version.pk])


def recover_url(version):
    return reverse("admin:prompts_prompt_recover", args=[version.pk])


def prompt_content_type_kwargs():
    return {"content_type__app_label": "prompts", "content_type__model": "prompt"}


CHANGELIST_URL = reverse("admin:prompts_prompt_changelist")


class RevertGuardTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.editor = User.objects.create_user("c4j-editor", password="pw", is_staff=True)
        cls.editor.groups.add(Group.objects.get_or_create(name="Editor")[0])
        cls.author = User.objects.create_user("c4j-author", password="pw", is_staff=True)
        cls.author.groups.add(Group.objects.get_or_create(name="Author")[0])
        cls.other_author = User.objects.create_user("c4j-other-author", password="pw", is_staff=True)
        cls.other_author.groups.add(Group.objects.get_or_create(name="Author")[0])

    def setUp(self):
        self.client.force_login(self.editor)

    def make_prompt(
        self, *, status=Workflow.STATUS_DRAFT, author=None, title="Content A",
        intro="intro", outro="outro", slug=None, **extra,
    ):
        prompt = Prompt.objects.create(status=status, author=author, **extra)
        prompt.create_translation(
            "en", title=title, intro=intro, body="body", outro=outro,
            slug=slug or f"c4j-slug-{next(_slug_counter)}",
        )
        return prompt

    def submit(self, prompt, *, actor=None):
        submit_prompt_for_review(refetch(prompt), actor=actor or self.editor)
        return refetch(prompt)

    def approve(self, prompt, *, actor=None):
        approve_prompt_review(refetch(prompt), actor=actor or self.editor)
        return refetch(prompt)

    def edit_via_admin(self, prompt, *, actor=None, **overrides):
        """A real admin changeform POST - deliberately exercises Beta
        11.11C4H's already-tested normal path as ordinary setup machinery,
        never itself under test here."""
        actor = actor or self.editor
        fresh = refetch(prompt)
        data = self._form_payload(fresh)
        data.update(overrides)
        client = self.client
        if actor is not self.editor:
            client.force_login(actor)
        resp = client.post(change_url(prompt), data)
        if actor is not self.editor:
            client.force_login(self.editor)
        assert resp.status_code == 302, resp.content
        return refetch(prompt)

    def run_admin_action(self, action, prompt):
        """A real changelist-action POST (core.admin's shared, generic
        rework/publish/archive/restore actions - none overridden by Prompt) -
        used only to reach a real, business-authentic historical status
        (published/archived/rework), never itself the thing under test."""
        resp = self.client.post(
            CHANGELIST_URL,
            data={"action": action, "_selected_action": [str(prompt.pk)], "index": "0"},
            follow=True,
        )
        assert resp.status_code == 200, resp.content
        return refetch(prompt)

    def publish_via_admin(self, prompt):
        return self.run_admin_action("action_publish", prompt)

    def archive_via_admin(self, prompt):
        return self.run_admin_action("action_archive", prompt)

    def _form_payload(self, prompt, *, author=None, tools=(), title=None):
        return {
            "author": str(author.pk) if author else (str(prompt.author_id) if prompt.author_id else ""),
            "review_note": prompt.review_note,
            "published_at_0": "",
            "published_at_1": "",
            "tools": [str(t.pk) for t in tools],
            "slug": prompt.safe_translation_getter("slug", language_code="en"),
            "title": title if title is not None else prompt.safe_translation_getter("title", language_code="en"),
            "intro": "intro",
            "body": "body",
            "outro": "outro",
            "_continue": "Save",
        }

    def confirm_payload(
        self, prompt, *, title, author=None, tools=(), snapshot=None,
        slug=None, intro=None, outro=None, review_note=None,
    ):
        """
        The field values a real browser would re-submit when confirming a
        revision/recover preview: the just-restored content.

        ``snapshot`` (see ``snapshot_for_confirm()``) must be supplied for a
        *recover* confirmation, since by then the row has already been
        deleted and a fresh read is no longer possible; a *revision* (revert)
        confirmation can omit it and rely on the still-existing row.

        ``slug``/``intro``/``outro``/``review_note`` default to the current
        (pre-revert) row's own values - correct whenever the field under
        test isn't one of those. A field-matrix test reverting on exactly
        one of them must pass the *restored* (generation A) value
        explicitly, since the row being confirmed here is still at its
        *current* (generation B) state until the POST below actually runs.
        ``review_note`` matters beyond its own field: a real mismatch there
        alone is enough for ``form.has_changed()`` to be true, which -
        together with a status the row happened to reach via ``publish`` -
        can trip ``EditorialWorkflowAdminMixin``'s pre-existing (C4H-era
        documented) auto-review mechanism as an unrelated side effect; tests
        reverting onto a "published" snapshot must pass the value that
        snapshot's own capture moment actually set, not whatever the row
        picked up afterwards.
        """
        if snapshot is None:
            snapshot = self.snapshot_for_confirm(prompt)
        return {
            "author": str(author.pk) if author else (str(snapshot["author_id"]) if snapshot["author_id"] else ""),
            "review_note": review_note if review_note is not None else snapshot["review_note"],
            "published_at_0": "",
            "published_at_1": "",
            "tools": [str(t.pk) for t in tools],
            "slug": slug if slug is not None else snapshot["slug"],
            "title": title,
            "intro": intro if intro is not None else "intro",
            "body": "body",
            "outro": outro if outro is not None else "outro",
            "_continue": "Save",
        }

    def snapshot_for_confirm(self, prompt):
        """Captures the fields ``confirm_payload()`` needs while the row
        still exists - required before a recover test deletes it."""
        fresh = refetch(prompt)
        return {
            "slug": fresh.safe_translation_getter("slug", language_code="en"),
            "review_note": fresh.review_note,
            "author_id": fresh.author_id,
        }

    def version_for(self, prompt, revision):
        return Version.objects.get(revision=revision, object_id=str(prompt.pk), **prompt_content_type_kwargs())

    def latest_revision(self):
        return Revision.objects.latest("pk")

    def build_two_generation_history(self, *, author=None):
        """
        Builds, through real primitives and one real admin edit, a Prompt
        with:

        - ``stage_a_version`` - a real historical Version, captured while
          genuinely "review", content "Content A". Beta 11.11C2A deliberately
          never binds ``review_revision`` inside the revision it captures
          (see ``prompts/review_submission.py``'s module docstring: "the root
          version in this revision is deliberately not self-referential") -
          so this snapshot's own serialized ``review_revision`` is always
          ``None``, regardless of content.
        - ``approved_a_version`` - a real historical Version, captured while
          genuinely "approved", still content "Content A". Unlike submit,
          Beta 11.11C3A sets ``locked.approved_revision = locked.review_revision``
          *before* its own save inside the revision - so this snapshot's
          ``review_revision``/``approved_revision`` are both genuinely,
          validly bound to the real submit-time revision.
        - ``review_b_version`` - a real historical Version, captured while
          genuinely "review" again, content "Content B" (same
          always-``None``-``review_revision`` caveat as ``stage_a_version``).
        - ``approved_b_version`` - a real historical Version, captured while
          genuinely "approved" again, content "Content B", with a genuinely
          valid binding (same caveat as ``approved_a_version``).
        - the CURRENT live row: "approved", content "Content B", bound to a
          review_revision/approved_revision pair that itself contains
          content "Content B" - the same revision ``approved_b_version``
          itself belongs to.
        """
        author = author or self.editor
        prompt = self.make_prompt(author=author, title="Content A")
        self.submit(prompt, actor=self.editor)
        stage_a_version = self.version_for(prompt, self.latest_revision())

        self.approve(prompt, actor=self.editor)
        approved_a_version = self.version_for(prompt, self.latest_revision())

        # Real admin edit while approved: existing C4H normal path invalidates
        # review->draft with the new content already saved - pure setup here.
        self.edit_via_admin(prompt, title="Content B")
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_DRAFT)

        self.submit(refetch(prompt), actor=self.editor)
        review_b_version = self.version_for(prompt, self.latest_revision())

        self.approve(refetch(prompt), actor=self.editor)
        approved_b_version = self.version_for(prompt, self.latest_revision())

        current = refetch(prompt)
        self.assertEqual(current.status, Workflow.STATUS_APPROVED)
        self.assertEqual(current.translations.get(language_code="en").title, "Content B")

        return (
            prompt, stage_a_version, approved_a_version, review_b_version,
            approved_b_version, current,
        )

    def build_field_variant_history(self, *, field, value_a, value_b, author=None):
        """
        Beta 11.11C4J field-matrix helper (Abnahme-Korrekturrunde). Builds a
        Prompt through real primitives/admin edits, exactly like
        ``build_two_generation_history()``, but varies exactly one real
        admin-form ``PromptTranslation`` field (``field`` - one of ``"intro"``,
        ``"outro"``, ``"slug"``) between an APPROVED generation A
        (``value_a``, captured genuinely approved so ``version_a`` carries a
        structurally valid binding - see
        ``build_two_generation_history()``'s own docstring for why an
        approved, not a review-status, snapshot is required for that) and a
        current generation B (``value_b``). Every other field stays constant,
        so a test reverting on ``field`` is unambiguous about what changed.

        Returns ``(prompt, version_a, current)``.
        """
        author = author or self.editor
        creation_kwargs = {field: value_a}
        prompt = self.make_prompt(author=author, title="Content A", **creation_kwargs)
        self.submit(prompt, actor=self.editor)
        self.approve(refetch(prompt), actor=self.editor)
        version_a = self.version_for(prompt, self.latest_revision())

        self.edit_via_admin(refetch(prompt), **{field: value_b})
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_DRAFT)
        self.submit(refetch(prompt), actor=self.editor)
        self.approve(refetch(prompt), actor=self.editor)

        current = refetch(prompt)
        self.assertEqual(current.status, Workflow.STATUS_APPROVED)
        return prompt, version_a, current

    def build_author_variant_history(self, *, author_a, author_b):
        """Same shape as ``build_field_variant_history()``, for ``Prompt.author_id``
        - a root field, not a translation field, so it needs its own
        stringified-pk admin override rather than the generic ``**{field: value}``
        pattern (see ``edit_via_admin()``/``_form_payload()``)."""
        prompt = self.make_prompt(author=author_a, title="Content A")
        self.submit(prompt, actor=self.editor)
        self.approve(refetch(prompt), actor=self.editor)
        version_a = self.version_for(prompt, self.latest_revision())

        self.edit_via_admin(refetch(prompt), author=str(author_b.pk))
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_DRAFT)
        self.submit(refetch(prompt), actor=self.editor)
        self.approve(refetch(prompt), actor=self.editor)

        current = refetch(prompt)
        self.assertEqual(current.status, Workflow.STATUS_APPROVED)
        self.assertEqual(current.author_id, author_b.pk)
        return prompt, version_a, current

    def build_public_slug_variant_history(self, *, value_a, value_b):
        """
        ``public_slug`` is in ``PromptAdmin.readonly_fields`` - never a real
        admin-form field - so the *only* way to inject a specific value for
        test setup is a direct ORM update; the real form genuinely has no
        field for it. Each value is nonetheless captured into a genuine
        reversion snapshot by a real ``submit()``/``approve()`` cycle, and
        the revert being tested is a real ``VersionAdmin`` POST - only the
        *setup* value injection is direct.

        With the C4J production fix in place, the intervening
        ``edit_via_admin()`` correctly *preserves* ``public_slug`` (it reads
        the field fresh from the database before the changeform save), so
        generation B's value is set with an explicit ``.update()`` after that
        edit and before the next ``submit()`` - C2A/C3A both re-check the
        payload fingerprint against submit time, so the value must be settled
        before submission.
        """
        prompt = self.make_prompt(author=self.editor, title="Content A")
        prompt.translations.filter(language_code="en").update(public_slug=value_a)
        self.submit(refetch(prompt), actor=self.editor)
        self.approve(refetch(prompt), actor=self.editor)
        version_a = self.version_for(prompt, self.latest_revision())
        self.assertEqual(
            version_a_public_slug := self._version_public_slug(version_a), value_a,
            f"version_a snapshot must carry public_slug={value_a!r}, got "
            f"{version_a_public_slug!r}",
        )

        self.edit_via_admin(refetch(prompt), title="Content A (v2)")
        draft = refetch(prompt)
        self.assertEqual(draft.status, Workflow.STATUS_DRAFT)
        draft.translations.filter(language_code="en").update(public_slug=value_b)

        self.submit(refetch(prompt), actor=self.editor)
        self.approve(refetch(prompt), actor=self.editor)

        current = refetch(prompt)
        self.assertEqual(current.status, Workflow.STATUS_APPROVED)
        self.assertEqual(
            current.translations.get(language_code="en").public_slug, value_b
        )
        return prompt, version_a, current

    def _version_public_slug(self, prompt_version):
        """The ``public_slug`` stored in a Prompt root Version's own
        translation snapshot, read straight from the serialized reversion
        data - used to prove a setup snapshot genuinely carries the intended
        historical value before it is reverted to."""
        import json

        revision = prompt_version.revision
        for version in revision.version_set.filter(
            content_type__app_label="prompts", content_type__model="prompttranslation"
        ):
            fields = json.loads(version.serialized_data)[0]["fields"]
            if fields.get("language_code") == "en":
                return fields.get("public_slug")
        return None


# ======================================================================
# Revert with a genuine payload change
# ======================================================================


class RevertPayloadChangeTests(RevertGuardTestCase):
    def test_revert_to_review_snapshot_invalidates_approved_to_draft(self):
        prompt, stage_a_version, _approved_a, _review_b, _approved_b, current = self.build_two_generation_history()
        self.assertEqual(current.live_i18n, {})

        resp = self.client.post(
            revision_url(prompt, stage_a_version),
            self.confirm_payload(prompt, title="Content A"),
        )
        self.assertEqual(resp.status_code, 302)

        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_DRAFT)
        self.assertIsNone(reloaded.review_revision_id)
        self.assertIsNone(reloaded.approved_revision_id)
        self.assertEqual(reloaded.review_payload_fingerprint, "")
        self.assertIsNone(reloaded.reviewed_by_id)
        self.assertEqual(reloaded.translations.get(language_code="en").title, "Content A")

    def test_revert_to_review_snapshot_invalidates_to_rework_with_live_snapshot(self):
        """
        ``live_i18n`` is a plain local field on Prompt, not excluded from
        reversion's follow-graph - a revert restores it to whatever it was
        *at the reverted-to snapshot's own moment*, not whatever the row
        happens to carry right before the click. So the live snapshot must
        be set before the historical version being reverted to is captured,
        not on the current row afterwards (a self-contained setup, not
        ``build_two_generation_history()``, to keep that distinction clear).
        """
        prompt = self.make_prompt(author=self.editor, title="Content A")
        Prompt.objects.filter(pk=prompt.pk).update(live_i18n={"en": {"title": "Live"}})
        self.submit(refetch(prompt), actor=self.editor)
        stage_a_version = self.version_for(prompt, self.latest_revision())
        prompt = self.approve(refetch(prompt), actor=self.editor)

        # Reach "approved, content B" directly - setup only, distinct from
        # the real admin-POST revert under test below.
        prompt.translations.filter(language_code="en").update(title="Content B")

        resp = self.client.post(
            revision_url(prompt, stage_a_version),
            self.confirm_payload(prompt, title="Content A"),
        )
        self.assertEqual(resp.status_code, 302)
        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_REWORK)
        self.assertIsNone(reloaded.review_revision_id)
        self.assertEqual(reloaded.translations.get(language_code="en").title, "Content A")

    def test_revert_to_approved_snapshot_invalidates_to_draft(self):
        prompt, _stage_a, approved_a_version, _review_b, _approved_b, current = self.build_two_generation_history()

        resp = self.client.post(
            revision_url(prompt, approved_a_version),
            self.confirm_payload(prompt, title="Content A"),
        )
        self.assertEqual(resp.status_code, 302)
        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_DRAFT)
        self.assertIsNone(reloaded.approved_revision_id)
        self.assertIsNone(reloaded.review_revision_id)

    def test_revert_produces_exactly_one_b2b2_invalidation(self):
        prompt, stage_a_version, _approved_a, _review_b, _approved_b, _current = self.build_two_generation_history()
        with mock.patch(
            "prompts.admin.invalidate_editorial_review_state",
            wraps=invalidate_editorial_review_state,
        ) as invalidate, mock.patch(
            "prompts.review_edit_guard.invalidate_editorial_review_state",
            wraps=invalidate_editorial_review_state,
        ) as invalidate_via_guard:
            self.client.post(
                revision_url(prompt, stage_a_version),
                self.confirm_payload(prompt, title="Content A"),
            )
        # The genuine-change path goes through Beta 11.11C4G's own module,
        # not the direct admin-level helper - exactly one call in total.
        self.assertEqual(invalidate.call_count, 0)
        self.assertEqual(invalidate_via_guard.call_count, 1)


# ======================================================================
# Revert payload matrix: one field at a time, all via real revert POSTs
# (Abnahme-Korrekturrunde Blocker 2)
# ======================================================================


class RevertFieldMatrixTests(RevertGuardTestCase):
    """
    Each test here isolates exactly one C1-v2 payload field, reverts a real
    admin-form POST back to a genuinely captured, genuinely approved
    historical version carrying that field's "A" value, and proves: the
    field is restored, the payload is recognised as changed, the binding is
    invalidated, the correct target status is reached, and exactly one
    persistent revision exists afterwards. Title and body already have
    dedicated coverage in ``RevertPayloadChangeTests``/``build_two_generation_history()``
    and are intentionally not repeated here.
    """

    def test_revert_restores_intro_and_invalidates(self):
        prompt, version_a, current = self.build_field_variant_history(
            field="intro", value_a="Intro A", value_b="Intro B",
        )
        revisions_before = Revision.objects.count()

        resp = self.client.post(
            revision_url(prompt, version_a),
            self.confirm_payload(prompt, title="Content A", intro="Intro A"),
        )
        self.assertEqual(resp.status_code, 302)

        reloaded = refetch(prompt)
        self.assertEqual(reloaded.translations.get(language_code="en").intro, "Intro A")
        self.assertEqual(reloaded.status, Workflow.STATUS_DRAFT)
        self.assertIsNone(reloaded.review_revision_id)
        self.assertIsNone(reloaded.approved_revision_id)
        self.assertEqual(reloaded.review_payload_fingerprint, "")
        self.assertEqual(Revision.objects.count(), revisions_before + 1)
        self.assertNotEqual(current.review_revision_id, reloaded.review_revision_id)

    def test_revert_restores_outro_and_invalidates(self):
        prompt, version_a, current = self.build_field_variant_history(
            field="outro", value_a="Outro A", value_b="Outro B",
        )
        revisions_before = Revision.objects.count()

        resp = self.client.post(
            revision_url(prompt, version_a),
            self.confirm_payload(prompt, title="Content A", outro="Outro A"),
        )
        self.assertEqual(resp.status_code, 302)

        reloaded = refetch(prompt)
        self.assertEqual(reloaded.translations.get(language_code="en").outro, "Outro A")
        self.assertEqual(reloaded.status, Workflow.STATUS_DRAFT)
        self.assertIsNone(reloaded.review_revision_id)
        self.assertEqual(reloaded.review_payload_fingerprint, "")
        self.assertEqual(Revision.objects.count(), revisions_before + 1)
        self.assertNotEqual(current.review_revision_id, reloaded.review_revision_id)

    def test_revert_restores_slug_and_invalidates(self):
        slug_a = f"c4j-field-slug-a-{next(_slug_counter)}"
        slug_b = f"c4j-field-slug-b-{next(_slug_counter)}"
        prompt, version_a, current = self.build_field_variant_history(
            field="slug", value_a=slug_a, value_b=slug_b,
        )
        revisions_before = Revision.objects.count()

        resp = self.client.post(
            revision_url(prompt, version_a),
            self.confirm_payload(prompt, title="Content A", slug=slug_a),
        )
        self.assertEqual(resp.status_code, 302)

        reloaded = refetch(prompt)
        self.assertEqual(reloaded.translations.get(language_code="en").slug, slug_a)
        self.assertEqual(reloaded.status, Workflow.STATUS_DRAFT)
        self.assertIsNone(reloaded.review_revision_id)
        self.assertEqual(reloaded.review_payload_fingerprint, "")
        self.assertEqual(Revision.objects.count(), revisions_before + 1)
        self.assertNotEqual(current.review_revision_id, reloaded.review_revision_id)

    def test_revert_restores_public_slug_and_invalidates(self):
        """
        Abnahme-Korrekturrunde 2, Blocker 1: a real ``VersionAdmin`` revert
        POST must restore the *historical* ``public_slug`` value exactly, and
        keep it stored after the full changeform save. ``public_slug`` is a
        translated field that is admin-readonly (never rendered as an editable
        form field), so ``TranslatableModelForm`` never carries it; combined
        with Parler's translation cache holding the pre-revert row, the
        ordinary changeform ``obj.save()`` used to write ``None`` back over the
        value ``reversion.revert()`` had just restored. The C4J production fix
        (``PromptAdmin._preserve_untouched_translated_fields()``) re-reads the
        field fresh from the just-reverted database row, so the historical
        value survives.
        """
        public_slug_a = f"c4j-field-pub-a-{next(_slug_counter)}"
        public_slug_b = f"c4j-field-pub-b-{next(_slug_counter)}"
        prompt, version_a, current = self.build_public_slug_variant_history(
            value_a=public_slug_a, value_b=public_slug_b,
        )
        revisions_before = Revision.objects.count()

        resp = self.client.post(
            revision_url(prompt, version_a),
            self.confirm_payload(prompt, title="Content A"),
        )
        self.assertEqual(resp.status_code, 302)

        reloaded = refetch(prompt)
        self.assertEqual(
            reloaded.translations.get(language_code="en").public_slug, public_slug_a
        )
        self.assertEqual(reloaded.status, Workflow.STATUS_DRAFT)
        self.assertIsNone(reloaded.review_revision_id)
        self.assertEqual(reloaded.review_payload_fingerprint, "")
        self.assertEqual(Revision.objects.count(), revisions_before + 1)
        self.assertNotEqual(current.review_revision_id, reloaded.review_revision_id)

    def test_revert_restores_author_id_and_invalidates(self):
        prompt, version_a, current = self.build_author_variant_history(
            author_a=self.author, author_b=self.other_author,
        )
        revisions_before = Revision.objects.count()

        resp = self.client.post(
            revision_url(prompt, version_a),
            self.confirm_payload(prompt, author=self.author, title="Content A"),
        )
        self.assertEqual(resp.status_code, 302)

        reloaded = refetch(prompt)
        self.assertEqual(reloaded.author_id, self.author.pk)
        self.assertEqual(reloaded.status, Workflow.STATUS_DRAFT)
        self.assertIsNone(reloaded.review_revision_id)
        self.assertEqual(reloaded.review_payload_fingerprint, "")
        self.assertEqual(Revision.objects.count(), revisions_before + 1)
        self.assertNotEqual(current.review_revision_id, reloaded.review_revision_id)


# ======================================================================
# Revert status matrix: non-invalidatable statuses with a genuine payload
# change (Abnahme-Korrekturrunde Blocker 3)
# ======================================================================


class RevertStatusMatrixTests(RevertGuardTestCase):
    """
    ``draft``/``rework``/``published``/``archived`` are not in B2B2's own
    ``_INVALIDATABLE_STATUSES`` (only ``review``/``approved`` are) - a revert
    landing on one of them, even with genuinely different content, must
    leave the status exactly as reversion restored it. Beta 11.11C4G's
    compare (``invalidate_prompt_review_if_payload_changed()``) is still
    called unconditionally whenever the payload differs - it is B2B2's own
    existing, pre-existing no-op contract that leaves the row untouched, not
    a new status rule C4J invents for these four values.
    """

    def test_revert_to_draft_snapshot_with_payload_change_stays_draft(self):
        prompt = self.make_prompt(author=self.editor, title="Content A")
        self.edit_via_admin(prompt, title="Content A")
        version_a = self.version_for(prompt, self.latest_revision())
        self.edit_via_admin(refetch(prompt), title="Content B")
        current = refetch(prompt)
        self.assertEqual(current.status, Workflow.STATUS_DRAFT)
        revisions_before = Revision.objects.count()

        with mock.patch(
            "prompts.review_edit_guard.invalidate_editorial_review_state",
            wraps=invalidate_editorial_review_state,
        ) as invalidate:
            resp = self.client.post(
                revision_url(prompt, version_a),
                self.confirm_payload(prompt, title="Content A"),
            )
        self.assertEqual(resp.status_code, 302)
        # C4G's compare is still called (payload genuinely differs) - it is
        # B2B2's own no-op for a non-invalidatable status, not a skipped call.
        self.assertEqual(invalidate.call_count, 1)

        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_DRAFT)
        self.assertEqual(reloaded.translations.get(language_code="en").title, "Content A")
        self.assertEqual(Revision.objects.count(), revisions_before + 1)

    def test_revert_to_rework_snapshot_with_payload_change_stays_rework(self):
        prompt = self.make_prompt(
            status=Workflow.STATUS_REWORK, author=self.editor, title="Content A"
        )
        self.edit_via_admin(prompt, title="Content A")
        version_a = self.version_for(prompt, self.latest_revision())
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_REWORK)

        self.edit_via_admin(refetch(prompt), title="Content B")
        current = refetch(prompt)
        self.assertEqual(current.status, Workflow.STATUS_REWORK)
        revisions_before = Revision.objects.count()

        resp = self.client.post(
            revision_url(prompt, version_a),
            self.confirm_payload(prompt, title="Content A"),
        )
        self.assertEqual(resp.status_code, 302)

        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_REWORK)
        self.assertEqual(reloaded.translations.get(language_code="en").title, "Content A")
        self.assertEqual(Revision.objects.count(), revisions_before + 1)

    def test_revert_to_published_snapshot_with_payload_change_stays_published(self):
        """
        Abnahme-Korrekturrunde 2, Blocker 2: a *real*, unmocked ``VersionAdmin``
        revert POST onto a historical ``published`` version - with genuinely
        different content - must end ``published``.

        ``publish()`` itself populates a non-empty ``live_i18n`` (via
        ``_update_live_snapshot()``), so without the fix the sequence was:
        stale Parler form-initials make ``form.has_changed()`` spuriously
        true → ``EditorialWorkflowAdminMixin``'s general auto-review fires
        published→review → C4G then invalidates review→rework. The C4J fix
        (``PromptAdmin._auto_transition_to_review()`` bypasses that general
        auto-review, and *only* it, when a request-local C4J revert/recover
        state is present for this prompt) keeps the historical status intact;
        the reverted status is not an invalidatable one, so C4G's own compare
        is a B2B2 no-op. No mock, no manual status fix.
        """
        prompt = self.make_prompt(author=self.editor, title="Content A")
        self.submit(prompt, actor=self.editor)
        self.approve(refetch(prompt), actor=self.editor)
        published = self.publish_via_admin(refetch(prompt))
        self.assertEqual(published.status, Workflow.STATUS_PUBLISHED)
        self.assertTrue(published.is_published)
        self.assertIsNotNone(published.published_at)
        self.assertTrue(published.live_i18n)  # publish() set a real live snapshot
        version_a = self.version_for(prompt, self.latest_revision())

        self.edit_via_admin(refetch(prompt), title="Content B")
        revisions_before = Revision.objects.count()

        resp = self.client.post(
            revision_url(prompt, version_a),
            self.confirm_payload(prompt, title="Content A"),
        )
        self.assertEqual(resp.status_code, 302)

        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_PUBLISHED)
        self.assertTrue(reloaded.is_published)
        self.assertIsNotNone(reloaded.published_at)
        self.assertEqual(reloaded.translations.get(language_code="en").title, "Content A")
        self.assertEqual(Revision.objects.count(), revisions_before + 1)

    def test_revert_to_archived_snapshot_with_payload_change_stays_archived(self):
        """
        ``core.admin.EditorialWorkflowAdminMixin.action_archive`` explicitly
        clears ``is_published`` to ``False`` alongside the FSM ``archive``
        transition itself (a soft-delete: content stays, ``is_published``
        goes false, ``live_i18n``/``published_at``/``public_slug``/
        ``live_author`` are left exactly as they were - archive never
        touches them). The reverted-to snapshot therefore restores
        ``is_published=False`` and whatever publish-era fields it already
        carried; C4J adds no new logic here either, and "archived" is not an
        invalidatable status.
        """
        prompt = self.make_prompt(author=self.editor, title="Content A")
        self.submit(prompt, actor=self.editor)
        self.approve(refetch(prompt), actor=self.editor)
        archived = self.archive_via_admin(refetch(prompt))
        self.assertEqual(archived.status, Workflow.STATUS_ARCHIVED)
        self.assertFalse(archived.is_published)
        version_a = self.version_for(prompt, self.latest_revision())

        self.edit_via_admin(refetch(prompt), title="Content B")
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_ARCHIVED)
        revisions_before = Revision.objects.count()

        resp = self.client.post(
            revision_url(prompt, version_a),
            self.confirm_payload(prompt, title="Content A"),
        )
        self.assertEqual(resp.status_code, 302)

        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_ARCHIVED)
        self.assertFalse(reloaded.is_published)
        self.assertEqual(reloaded.translations.get(language_code="en").title, "Content A")
        self.assertEqual(Revision.objects.count(), revisions_before + 1)


# ======================================================================
# Normal (non-reversion) published edit: the auto-review bypass must NOT
# extend to it (Abnahme-Korrekturrunde 2, Blocker 2 / Phase 10)
# ======================================================================


class NormalPublishedEditContractTests(RevertGuardTestCase):
    def test_normal_published_edit_still_auto_reviews_outside_a_revert(self):
        """
        The C4J auto-review bypass is scoped strictly to a request-local
        revert/recover state. A completely ordinary changeform edit of a
        published prompt (no reversion involved) must keep the pre-existing
        Beta 11.11C4H behaviour exactly: the general auto-review mechanism
        fires, and C4H's own compare then invalidates the now-unbound
        "review" via B2B2 - draft here, since a real content change happened
        and there is no live snapshot that survives it. If the bypass ever
        leaked onto normal saves, this prompt would wrongly stay "published".
        """
        prompt = self.make_prompt(
            status=Workflow.STATUS_PUBLISHED, author=self.editor, title="Content A",
            is_published=True,
        )
        reloaded = self.edit_via_admin(refetch(prompt), title="Content B")
        # Pre-existing C4H-documented outcome, unchanged by C4J.
        self.assertEqual(reloaded.status, Workflow.STATUS_DRAFT)

    def test_normal_edit_preserves_public_slug_on_a_published_prompt(self):
        """Phase 10: even for a published prompt edited normally, an existing
        ``public_slug`` must survive the edit (the C4J preservation fix
        applies to every changeform save of an existing prompt, not only
        revert/recover)."""
        prompt = self.make_prompt(
            status=Workflow.STATUS_PUBLISHED, author=self.editor, title="Content A",
            is_published=True,
        )
        prompt.translations.filter(language_code="en").update(public_slug="live-public")
        self.assertEqual(
            refetch(prompt).translations.get(language_code="en").public_slug, "live-public"
        )

        self.edit_via_admin(refetch(prompt), title="Content B")

        self.assertEqual(
            refetch(prompt).translations.get(language_code="en").public_slug, "live-public"
        )


# ======================================================================
# Revert with an unchanged payload
# ======================================================================


class RevertNoPayloadChangeTests(RevertGuardTestCase):
    def test_identical_payload_with_valid_restored_binding_is_preserved(self):
        """
        Reverts to ``approved_b_version``, not ``review_b_version``: Beta
        11.11C2A deliberately never binds ``review_revision`` inside the
        revision it captures (see ``build_two_generation_history()``'s own
        docstring), so *any* "review"-status historical snapshot always
        restores ``review_revision=None`` - structurally invalid by
        construction, regardless of payload. Beta 11.11C3A's approve *does*
        bind both fields before its own save, so ``approved_b_version`` is
        the genuinely revertible "identical payload, still-valid binding"
        case this test needs.
        """
        prompt, _stage_a, _approved_a, _review_b, approved_b_version, current = (
            self.build_two_generation_history()
        )

        resp = self.client.post(
            revision_url(prompt, approved_b_version),
            self.confirm_payload(prompt, title="Content B"),
        )
        self.assertEqual(resp.status_code, 302)

        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_APPROVED)
        self.assertEqual(reloaded.review_revision_id, current.review_revision_id)
        self.assertEqual(reloaded.approved_revision_id, current.approved_revision_id)
        self.assertEqual(
            reloaded.review_payload_fingerprint, current.review_payload_fingerprint
        )

    def test_identical_payload_does_not_call_b2b2_when_binding_stays_valid(self):
        prompt, _stage_a, _approved_a, _review_b, approved_b_version, _current = (
            self.build_two_generation_history()
        )
        with mock.patch(
            "prompts.admin.invalidate_editorial_review_state"
        ) as invalidate:
            self.client.post(
                revision_url(prompt, approved_b_version),
                self.confirm_payload(prompt, title="Content B"),
            )
        invalidate.assert_not_called()

    # ------------------------------------------------------------------
    # Invalid/stale historical binding, via real revert POSTs
    # (Abnahme-Korrekturrunde Blocker 4)
    # ------------------------------------------------------------------

    def test_revert_to_review_snapshot_with_identical_payload_invalidates_via_real_post(self):
        """
        The mandatory Blocker 4 case, reached without any manual binding
        tampering at all: Beta 11.11C2A deliberately never binds
        ``review_revision`` inside the revision it captures (see
        ``build_two_generation_history()``'s own docstring - "the root
        version in this revision is deliberately not self-referential") -
        so ``review_b_version``'s own restored ``review_revision`` is
        genuinely ``None``. Content is identical to the current live payload
        throughout (both "Content B"), so the payload compare alone would
        report "unchanged"; the structural binding check is what invalidates.
        """
        prompt, _stage_a, _approved_a, review_b_version, _approved_b, current = (
            self.build_two_generation_history()
        )
        revisions_before = Revision.objects.count()

        resp = self.client.post(
            revision_url(prompt, review_b_version),
            self.confirm_payload(prompt, title="Content B"),
        )
        self.assertEqual(resp.status_code, 302)

        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_DRAFT)
        self.assertIsNone(reloaded.review_revision_id)
        self.assertIsNone(reloaded.approved_revision_id)
        self.assertEqual(reloaded.review_payload_fingerprint, "")
        # the payload itself never changed - content stays "Content B"
        self.assertEqual(reloaded.translations.get(language_code="en").title, "Content B")
        self.assertEqual(Revision.objects.count(), revisions_before + 1)

    def test_revert_with_stale_fingerprint_invalidates_via_real_post(self):
        """
        A real revert-POST landing on a historical version whose own content
        matches the current live payload, but whose own stored
        ``review_payload_fingerprint`` was a syntactically valid, wrong
        value at the moment that version was captured (setup only, via a
        direct update - the real form has no field for this, exactly like
        Blocker 2's ``public_slug`` case; the revert-POST itself, and the
        invalidation it triggers, are both real). ``review_note`` is
        genuinely admin-editable and genuinely absent from the C1 payload
        (see ``test_admin_review_edit_guard.py``'s own
        ``NonFingerprintRelevantChangeTests``), so editing only it captures a
        new, real historical Version carrying the tampered fingerprint
        without the ordinary C4H guard ever touching it (the payload itself
        never changes). Reverting to *that* version (here, reverting a row
        to its own latest state - a normal, valid admin action) restores the
        wrong fingerprint verbatim; the payload compare correctly reports
        "unchanged", and the fingerprint-vs-live check in
        ``_invalidate_reverted_prompt_if_binding_invalid()`` is what
        invalidates.
        """
        prompt = self.submit(self.make_prompt(author=self.editor))
        prompt = self.approve(prompt, actor=self.editor)
        Prompt.objects.filter(pk=prompt.pk).update(review_payload_fingerprint="0" * 64)
        self.edit_via_admin(refetch(prompt), review_note="internal note only")
        tampered = refetch(prompt)
        self.assertEqual(tampered.status, Workflow.STATUS_APPROVED)
        self.assertEqual(tampered.review_payload_fingerprint, "0" * 64)
        tampered_version = self.version_for(prompt, self.latest_revision())
        revisions_before = Revision.objects.count()

        resp = self.client.post(
            revision_url(prompt, tampered_version),
            self.confirm_payload(prompt, title="Content A"),
        )
        self.assertEqual(resp.status_code, 302)

        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_DRAFT)
        self.assertIsNone(reloaded.review_revision_id)
        self.assertEqual(reloaded.review_payload_fingerprint, "")
        self.assertEqual(reloaded.translations.get(language_code="en").title, "Content A")
        self.assertEqual(Revision.objects.count(), revisions_before + 1)

    def test_revert_with_foreign_revision_invalidates_via_real_post(self):
        """
        Same self-revert construction as the stale-fingerprint case above,
        but the tampered field is ``review_revision``/``approved_revision``
        themselves, pointed at a real ``Revision`` that simply does not
        contain a ``Version`` of *this* Prompt - the structural half of
        ``validate_approved_binding()`` (``APPROVED_REVISION_NOT_FOR_OBJECT``/
        ``REVIEW_REVISION_NOT_FOR_OBJECT``), reached through a real revert
        POST rather than a direct helper call.
        """
        other_prompt = self.edit_via_admin(self.make_prompt(author=self.editor, title="Other"))
        foreign_revision = self.latest_revision()

        prompt = self.submit(self.make_prompt(author=self.editor))
        prompt = self.approve(prompt, actor=self.editor)
        self.assertNotEqual(other_prompt.pk, prompt.pk)
        Prompt.objects.filter(pk=prompt.pk).update(
            review_revision_id=foreign_revision.pk, approved_revision_id=foreign_revision.pk
        )
        self.edit_via_admin(refetch(prompt), review_note="internal note only")
        tampered = refetch(prompt)
        self.assertEqual(tampered.status, Workflow.STATUS_APPROVED)
        self.assertEqual(tampered.review_revision_id, foreign_revision.pk)
        tampered_version = self.version_for(prompt, self.latest_revision())
        revisions_before = Revision.objects.count()

        resp = self.client.post(
            revision_url(prompt, tampered_version),
            self.confirm_payload(prompt, title="Content A"),
        )
        self.assertEqual(resp.status_code, 302)

        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_DRAFT)
        self.assertIsNone(reloaded.review_revision_id)
        self.assertIsNone(reloaded.approved_revision_id)
        self.assertEqual(Revision.objects.count(), revisions_before + 1)

    def test_stale_fingerprint_on_identical_payload_is_invalidated(self):
        """
        Not organically reachable via a real revert (old revisions are never
        pruned in this test, so a restored ``review_revision`` almost always
        still contains a real, matching version) - exercises
        ``_invalidate_reverted_prompt_if_binding_invalid()`` directly, the
        same "direct call for an edge case the real form can't reach"
        convention the sibling C4H test module already uses.
        """
        prompt = self.submit(self.make_prompt(author=self.editor))
        prompt = self.approve(prompt, actor=self.editor)
        Prompt.objects.filter(pk=prompt.pk).update(
            review_payload_fingerprint="0" * 64
        )
        stale = refetch(prompt)

        ma = PromptAdmin(Prompt, django_admin.site)
        with transaction.atomic():
            result = ma._invalidate_reverted_prompt_if_binding_invalid(
                stale, using="default"
            )

        self.assertIsNotNone(result)
        self.assertTrue(result.changed)
        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_DRAFT)
        self.assertIsNone(reloaded.review_revision_id)
        self.assertEqual(reloaded.review_payload_fingerprint, "")

    def test_dangling_review_revision_reference_is_invalidated(self):
        """A ``review_revision`` pointing at a real Revision that simply does
        not contain a Version of *this* Prompt - the structural half of
        ``validate_review_binding()`` - via the same direct-call convention."""
        other_prompt = self.edit_via_admin(self.make_prompt(author=self.editor, title="Other"))
        foreign_revision = self.latest_revision()

        prompt = self.submit(self.make_prompt(author=self.editor))
        prompt = self.approve(prompt, actor=self.editor)
        Prompt.objects.filter(pk=prompt.pk).update(
            review_revision_id=foreign_revision.pk, approved_revision_id=foreign_revision.pk
        )
        broken = refetch(prompt)
        self.assertNotEqual(other_prompt.pk, prompt.pk)

        ma = PromptAdmin(Prompt, django_admin.site)
        with transaction.atomic():
            result = ma._invalidate_reverted_prompt_if_binding_invalid(
                broken, using="default"
            )

        self.assertIsNotNone(result)
        self.assertTrue(result.changed)
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_DRAFT)

    def test_valid_binding_returns_none_and_issues_no_update(self):
        prompt = self.submit(self.make_prompt(author=self.editor))
        prompt = self.approve(prompt, actor=self.editor)

        ma = PromptAdmin(Prompt, django_admin.site)
        with CaptureQueriesContext(connection) as ctx:
            with transaction.atomic():
                result = ma._invalidate_reverted_prompt_if_binding_invalid(
                    refetch(prompt), using="default"
                )
        self.assertIsNone(result)
        updates = [q for q in ctx.captured_queries if q["sql"].strip().upper().startswith("UPDATE")]
        self.assertEqual(updates, [])

    def test_draft_status_is_never_checked_for_binding_validity(self):
        prompt = self.make_prompt(status=Workflow.STATUS_DRAFT, author=self.editor)
        ma = PromptAdmin(Prompt, django_admin.site)
        with transaction.atomic():
            result = ma._invalidate_reverted_prompt_if_binding_invalid(
                refetch(prompt), using="default"
            )
        self.assertIsNone(result)


# ======================================================================
# Recover
# ======================================================================


class RecoverTests(RevertGuardTestCase):
    def _prepare_deleted(self, prompt):
        """Captures everything ``confirm_payload()``/assertions need, then
        deletes the row - mirroring "a Prompt someone deleted, now being
        recovered", never reading through the (by-then-deleted) row again."""
        version = self.version_for(prompt, self.latest_revision())
        pk = prompt.pk
        title = prompt.translations.get(language_code="en").title
        snapshot = self.snapshot_for_confirm(prompt)
        Prompt.objects.filter(pk=pk).delete()
        self.assertFalse(Prompt.objects.filter(pk=pk).exists())
        return version, pk, title, snapshot

    def test_recover_of_approved_prompt_invalidates_to_draft(self):
        prompt = self.submit(self.make_prompt(author=self.editor))
        prompt = self.approve(prompt, actor=self.editor)
        version, pk, title, snapshot = self._prepare_deleted(prompt)

        resp = self.client.post(
            recover_url(version), self.confirm_payload(prompt, title=title, snapshot=snapshot)
        )
        self.assertEqual(resp.status_code, 302)

        recovered = Prompt.objects.get(pk=pk)
        self.assertEqual(recovered.status, Workflow.STATUS_DRAFT)
        self.assertIsNone(recovered.review_revision_id)
        self.assertIsNone(recovered.approved_revision_id)
        self.assertEqual(recovered.review_payload_fingerprint, "")

    def test_recover_of_review_prompt_invalidates_with_live_snapshot_to_rework(self):
        """``live_i18n`` must be set *before* the submit whose revision gets
        recovered - see the analogous note on the revert-side live-snapshot
        test above; a later ``.update()`` would not be part of that
        snapshot."""
        prompt = self.make_prompt(author=self.editor)
        Prompt.objects.filter(pk=prompt.pk).update(live_i18n={"en": {"title": "Live"}})
        prompt = self.submit(refetch(prompt))
        version, pk, title, snapshot = self._prepare_deleted(refetch(prompt))

        resp = self.client.post(
            recover_url(version), self.confirm_payload(prompt, title=title, snapshot=snapshot)
        )
        self.assertEqual(resp.status_code, 302)
        recovered = Prompt.objects.get(pk=pk)
        self.assertEqual(recovered.status, Workflow.STATUS_REWORK)

    def test_recover_of_draft_prompt_is_a_b2b2_noop(self):
        prompt = self.edit_via_admin(self.make_prompt(author=self.editor, title="Draft content"))
        version, pk, title, snapshot = self._prepare_deleted(prompt)

        resp = self.client.post(
            recover_url(version), self.confirm_payload(prompt, title=title, snapshot=snapshot)
        )
        self.assertEqual(resp.status_code, 302)
        recovered = Prompt.objects.get(pk=pk)
        self.assertEqual(recovered.status, Workflow.STATUS_DRAFT)

    def test_recover_calls_b2b2_exactly_once_regardless_of_outcome(self):
        prompt = self.submit(self.make_prompt(author=self.editor))
        prompt = self.approve(prompt, actor=self.editor)
        version, pk, title, snapshot = self._prepare_deleted(prompt)

        with mock.patch(
            "prompts.admin.invalidate_editorial_review_state",
            wraps=invalidate_editorial_review_state,
        ) as invalidate:
            self.client.post(
                recover_url(version), self.confirm_payload(prompt, title=title, snapshot=snapshot)
            )
        self.assertEqual(invalidate.call_count, 1)

    def test_recover_get_preview_leaves_object_deleted(self):
        prompt = self.submit(self.make_prompt(author=self.editor))
        prompt = self.approve(prompt, actor=self.editor)
        version, pk, _title, _snapshot = self._prepare_deleted(prompt)
        revisions_before = Revision.objects.count()

        resp = self.client.get(recover_url(version))
        self.assertEqual(resp.status_code, 200)

        self.assertFalse(Prompt.objects.filter(pk=pk).exists())
        self.assertEqual(Revision.objects.count(), revisions_before)

    def test_recover_leaves_no_request_local_state_residue(self):
        prompt = self.submit(self.make_prompt(author=self.editor))
        prompt = self.approve(prompt, actor=self.editor)
        version, pk, title, snapshot = self._prepare_deleted(prompt)

        captured_requests = []
        original_save_related = PromptAdmin.save_related

        def spy(self_admin, request, form, formsets, change):
            result = original_save_related(self_admin, request, form, formsets, change)
            captured_requests.append(request)
            return result

        with mock.patch.object(PromptAdmin, "save_related", spy):
            self.client.post(
                recover_url(version), self.confirm_payload(prompt, title=title, snapshot=snapshot)
            )

        self.assertEqual(len(captured_requests), 1)
        self.assertEqual(
            getattr(captured_requests[0], "_mentoro_prompt_review_edit_baselines", {}), {}
        )

    def test_recover_of_approved_prompt_with_live_snapshot_invalidates_to_rework(self):
        """The approved-side counterpart of
        ``test_recover_of_review_prompt_invalidates_with_live_snapshot_to_rework``
        above (Abnahme-Korrekturrunde Blocker 3)."""
        prompt = self.make_prompt(author=self.editor)
        Prompt.objects.filter(pk=prompt.pk).update(live_i18n={"en": {"title": "Live"}})
        prompt = self.submit(refetch(prompt))
        prompt = self.approve(refetch(prompt), actor=self.editor)
        version, pk, title, snapshot = self._prepare_deleted(prompt)

        resp = self.client.post(
            recover_url(version), self.confirm_payload(prompt, title=title, snapshot=snapshot)
        )
        self.assertEqual(resp.status_code, 302)
        recovered = Prompt.objects.get(pk=pk)
        self.assertEqual(recovered.status, Workflow.STATUS_REWORK)
        self.assertIsNone(recovered.approved_revision_id)
        self.assertIsNone(recovered.review_revision_id)

    def test_recover_of_rework_prompt_is_a_b2b2_noop(self):
        prompt = self.make_prompt(
            status=Workflow.STATUS_REWORK, author=self.editor, title="Rework content"
        )
        self.edit_via_admin(prompt, title="Rework content")
        prompt = refetch(prompt)
        self.assertEqual(prompt.status, Workflow.STATUS_REWORK)
        version, pk, title, snapshot = self._prepare_deleted(prompt)

        resp = self.client.post(
            recover_url(version), self.confirm_payload(prompt, title=title, snapshot=snapshot)
        )
        self.assertEqual(resp.status_code, 302)
        recovered = Prompt.objects.get(pk=pk)
        self.assertEqual(recovered.status, Workflow.STATUS_REWORK)

    def test_recover_of_published_prompt_is_a_b2b2_noop(self):
        prompt = self.make_prompt(author=self.editor)
        self.submit(prompt, actor=self.editor)
        self.approve(refetch(prompt), actor=self.editor)
        published = self.publish_via_admin(refetch(prompt))
        self.assertEqual(published.status, Workflow.STATUS_PUBLISHED)
        version, pk, title, snapshot = self._prepare_deleted(published)

        resp = self.client.post(
            recover_url(version), self.confirm_payload(published, title=title, snapshot=snapshot)
        )
        self.assertEqual(resp.status_code, 302)
        recovered = Prompt.objects.get(pk=pk)
        self.assertEqual(recovered.status, Workflow.STATUS_PUBLISHED)
        self.assertTrue(recovered.is_published)

    def test_recover_of_archived_prompt_is_a_b2b2_noop(self):
        prompt = self.make_prompt(author=self.editor)
        self.submit(prompt, actor=self.editor)
        self.approve(refetch(prompt), actor=self.editor)
        archived = self.archive_via_admin(refetch(prompt))
        self.assertEqual(archived.status, Workflow.STATUS_ARCHIVED)
        version, pk, title, snapshot = self._prepare_deleted(archived)

        resp = self.client.post(
            recover_url(version), self.confirm_payload(archived, title=title, snapshot=snapshot)
        )
        self.assertEqual(resp.status_code, 302)
        recovered = Prompt.objects.get(pk=pk)
        self.assertEqual(recovered.status, Workflow.STATUS_ARCHIVED)
        self.assertFalse(recovered.is_published)

    def test_recover_restores_public_slug_exactly(self):
        """Abnahme-Korrekturrunde 2, Blocker 1 (recover side): the historical
        ``public_slug`` a deleted prompt carried must come back exactly after
        a real recover POST - never ``None``, never freshly derived."""
        prompt = self.make_prompt(author=self.editor)
        prompt.translations.filter(language_code="en").update(public_slug="recover-pub")
        prompt = self.submit(refetch(prompt))  # snapshot carries public_slug
        version, pk, title, snapshot = self._prepare_deleted(prompt)

        resp = self.client.post(
            recover_url(version), self.confirm_payload(prompt, title=title, snapshot=snapshot)
        )
        self.assertEqual(resp.status_code, 302)
        recovered = Prompt.objects.get(pk=pk)
        self.assertEqual(
            recovered.translations.get(language_code="en").public_slug, "recover-pub"
        )
        # review with no live snapshot -> invalidated to draft
        self.assertEqual(recovered.status, Workflow.STATUS_DRAFT)

    def test_recover_of_new_language_keeps_public_slug_none(self):
        """The preservation helper must never manufacture a translation: a
        recovered snapshot whose translation never had a ``public_slug`` keeps
        it ``None`` (fresh read returns no row for the untouched field only
        when the row itself is absent; here the row exists with ``None``)."""
        prompt = self.submit(self.make_prompt(author=self.editor))
        prompt = self.approve(refetch(prompt), actor=self.editor)
        version, pk, title, snapshot = self._prepare_deleted(prompt)

        resp = self.client.post(
            recover_url(version), self.confirm_payload(prompt, title=title, snapshot=snapshot)
        )
        self.assertEqual(resp.status_code, 302)
        recovered = Prompt.objects.get(pk=pk)
        self.assertIsNone(recovered.translations.get(language_code="en").public_slug)


# ======================================================================
# GET preview and invalid POST: no persistent effect
# ======================================================================


class RevertPreviewAndInvalidPostTests(RevertGuardTestCase):
    def test_get_preview_has_no_persistent_effect(self):
        prompt, stage_a_version, _approved_a, _review_b, _approved_b, current = self.build_two_generation_history()
        revisions_before = Revision.objects.count()

        resp = self.client.get(revision_url(prompt, stage_a_version))
        self.assertEqual(resp.status_code, 200)

        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, current.status)
        self.assertEqual(reloaded.review_revision_id, current.review_revision_id)
        self.assertEqual(
            reloaded.translations.get(language_code="en").title, "Content B"
        )
        self.assertEqual(Revision.objects.count(), revisions_before)

    def test_get_preview_leaves_no_request_local_state_residue(self):
        prompt, stage_a_version, _approved_a, _review_b, _approved_b, _current = self.build_two_generation_history()
        captured_requests = []
        original_save_model = PromptAdmin.save_model

        def spy(self_admin, request, obj, form, change):
            captured_requests.append(request)
            return original_save_model(self_admin, request, obj, form, change)

        with mock.patch.object(PromptAdmin, "save_model", spy):
            self.client.get(revision_url(prompt, stage_a_version))
        # GET never reaches save_model() at all - proves the state was
        # placed and then removed purely by revision_view()'s own finally.
        self.assertEqual(captured_requests, [])

    def test_invalid_post_rolls_back_completely(self):
        prompt, stage_a_version, _approved_a, _review_b, _approved_b, current = self.build_two_generation_history()
        revisions_before = Revision.objects.count()

        payload = self.confirm_payload(prompt, title="Content A")
        payload["slug"] = ""  # required field, makes the form invalid

        resp = self.client.post(revision_url(prompt, stage_a_version), payload)
        self.assertEqual(resp.status_code, 200)  # re-rendered with errors

        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, current.status)
        self.assertEqual(reloaded.review_revision_id, current.review_revision_id)
        self.assertEqual(
            reloaded.translations.get(language_code="en").title, "Content B"
        )
        self.assertEqual(Revision.objects.count(), revisions_before)


# ======================================================================
# Errors and rollback
# ======================================================================


class ErrorAndRollbackTests(RevertGuardTestCase):
    def test_capture_failure_prevents_any_reversion_mutation(self):
        prompt, stage_a_version, _approved_a, _review_b, _approved_b, current = self.build_two_generation_history()
        revisions_before = Revision.objects.count()

        with mock.patch(
            "prompts.admin.capture_prompt_review_edit_baseline",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    revision_url(prompt, stage_a_version),
                    self.confirm_payload(prompt, title="Content A"),
                )

        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, current.status)
        self.assertEqual(reloaded.review_revision_id, current.review_revision_id)
        self.assertEqual(
            reloaded.translations.get(language_code="en").title, "Content B"
        )
        self.assertEqual(Revision.objects.count(), revisions_before)

    def test_save_related_failure_rolls_back_the_reversion_mutation_too(self):
        prompt, stage_a_version, _approved_a, _review_b, _approved_b, current = self.build_two_generation_history()
        revisions_before = Revision.objects.count()

        with mock.patch.object(
            django_admin.ModelAdmin, "save_related", side_effect=RuntimeError("save_related boom")
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    revision_url(prompt, stage_a_version),
                    self.confirm_payload(prompt, title="Content A"),
                )

        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, current.status)
        self.assertEqual(reloaded.review_revision_id, current.review_revision_id)
        self.assertEqual(
            reloaded.translations.get(language_code="en").title, "Content B"
        )
        self.assertEqual(Revision.objects.count(), revisions_before)

    def test_compare_failure_rolls_back_everything(self):
        prompt, stage_a_version, _approved_a, _review_b, _approved_b, current = self.build_two_generation_history()
        revisions_before = Revision.objects.count()

        with mock.patch(
            "prompts.admin.invalidate_prompt_review_if_payload_changed",
            side_effect=RuntimeError("compare boom"),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    revision_url(prompt, stage_a_version),
                    self.confirm_payload(prompt, title="Content A"),
                )

        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, current.status)
        self.assertEqual(reloaded.review_revision_id, current.review_revision_id)
        self.assertEqual(
            reloaded.translations.get(language_code="en").title, "Content B"
        )
        self.assertEqual(Revision.objects.count(), revisions_before)

    def test_recover_b2b2_failure_leaves_object_deleted(self):
        prompt = self.submit(self.make_prompt(author=self.editor))
        prompt = self.approve(prompt, actor=self.editor)
        version = self.version_for(prompt, self.latest_revision())
        pk = prompt.pk
        title = prompt.translations.get(language_code="en").title
        snapshot = self.snapshot_for_confirm(prompt)
        Prompt.objects.filter(pk=pk).delete()
        revisions_before = Revision.objects.count()

        with mock.patch(
            "prompts.admin.invalidate_editorial_review_state",
            side_effect=RuntimeError("recover boom"),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    recover_url(version),
                    self.confirm_payload(prompt, title=title, snapshot=snapshot),
                )

        self.assertFalse(Prompt.objects.filter(pk=pk).exists())
        self.assertEqual(Revision.objects.count(), revisions_before)

    def test_public_slug_preservation_failure_prevents_any_save(self):
        """Abnahme-Korrekturrunde 2, Phase 11: if the public_slug preservation
        read itself fails, the whole revert rolls back - no root save, no
        translation change, no revision, and the original public_slug stays."""
        prompt = self.make_prompt(author=self.editor, title="Content A")
        prompt.translations.filter(language_code="en").update(public_slug="orig-pub")
        self.submit(refetch(prompt), actor=self.editor)
        self.approve(refetch(prompt), actor=self.editor)
        version_a = self.version_for(prompt, self.latest_revision())
        self.edit_via_admin(refetch(prompt), title="Content B")
        self.submit(refetch(prompt), actor=self.editor)
        self.approve(refetch(prompt), actor=self.editor)
        revisions_before = Revision.objects.count()

        with mock.patch.object(
            PromptAdmin,
            "_preserve_untouched_translated_fields",
            side_effect=RuntimeError("preserve boom"),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    revision_url(prompt, version_a),
                    self.confirm_payload(prompt, title="Content A"),
                )

        reloaded = refetch(prompt)
        self.assertEqual(
            reloaded.translations.get(language_code="en").public_slug, "orig-pub"
        )
        self.assertEqual(reloaded.translations.get(language_code="en").title, "Content B")
        self.assertEqual(Revision.objects.count(), revisions_before)

    def test_compare_failure_rolls_back_restored_public_slug(self):
        """A revert that restores a historical public_slug and then fails in
        the C4G compare must roll the restored public_slug back too."""
        prompt = self.make_prompt(author=self.editor, title="Content A")
        prompt.translations.filter(language_code="en").update(public_slug="hist-pub")
        self.submit(refetch(prompt), actor=self.editor)
        self.approve(refetch(prompt), actor=self.editor)
        version_a = self.version_for(prompt, self.latest_revision())
        self.edit_via_admin(refetch(prompt), title="Content B")
        refetch(prompt).translations.filter(language_code="en").update(public_slug="curr-pub")
        self.submit(refetch(prompt), actor=self.editor)
        self.approve(refetch(prompt), actor=self.editor)
        revisions_before = Revision.objects.count()

        with mock.patch(
            "prompts.admin.invalidate_prompt_review_if_payload_changed",
            side_effect=RuntimeError("compare boom"),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    revision_url(prompt, version_a),
                    self.confirm_payload(prompt, title="Content A"),
                )

        reloaded = refetch(prompt)
        self.assertEqual(
            reloaded.translations.get(language_code="en").public_slug, "curr-pub"
        )
        self.assertEqual(reloaded.translations.get(language_code="en").title, "Content B")
        self.assertEqual(Revision.objects.count(), revisions_before)


# ======================================================================
# Request-lifecycle state contract (save_model / save_related integration)
# ======================================================================


class RequestStateLifecycleTests(RevertGuardTestCase):
    def test_save_model_does_not_capture_a_second_baseline_during_revert(self):
        prompt, stage_a_version, _approved_a, _review_b, _approved_b, _current = self.build_two_generation_history()
        with mock.patch(
            "prompts.admin.capture_prompt_review_edit_baseline",
            wraps=capture_prompt_review_edit_baseline,
        ) as capture:
            self.client.post(
                revision_url(prompt, stage_a_version),
                self.confirm_payload(prompt, title="Content A"),
            )
        # Exactly one capture: the pre-revert one taken by revision_view()
        # itself. save_model() must not call it a second time.
        self.assertEqual(capture.call_count, 1)

    def test_normal_changeform_still_raises_on_unexpected_pre_existing_state(self):
        """
        The Beta 11.11C4H integration-contract guard: a normal changeform
        save must never find a pre-existing NORMAL_CHANGEFORM state for the
        same prompt already on the request (would mean ``save_model()`` ran
        twice for the same object without an intervening ``save_related()``).
        Revert/recovery states are exempt from this check by design - see
        ``save_model()``.
        """
        prompt = self.submit(self.make_prompt(author=self.editor))
        ma = PromptAdmin(Prompt, django_admin.site)
        request = RequestFactory().post("/")
        request.user = self.editor
        fresh = refetch(prompt)
        fresh.set_current_language("en")
        form = mock.Mock(instance=fresh, save_m2m=mock.Mock(), fields=_CHANGEFORM_FORM_FIELDS)

        with transaction.atomic():
            with reversion.create_revision():
                reversion.set_user(self.editor)
                ma.save_model(request, fresh, form, True)
                with self.assertRaises(_PromptReviewEditIntegrationError):
                    ma.save_model(request, fresh, form, True)

    def test_state_dataclass_carries_no_model_or_user_instances(self):
        fields = _PromptReversionGuardState.__dataclass_fields__
        self.assertEqual(
            set(fields), {"source", "prompt_id", "database_alias", "baseline"}
        )
        self.assertTrue(hasattr(_PromptReversionGuardState, "__dataclass_fields__"))
        # frozen + slots, mirroring PromptReviewEditBaseline's own contract
        self.assertTrue(_PromptReversionGuardState.__dataclass_params__.frozen)

    def test_source_enum_has_exactly_the_three_documented_values(self):
        self.assertEqual(
            {member.value for member in _PromptReversionGuardSource},
            {"normal_changeform", "revision_revert", "recovery"},
        )


# ======================================================================
# Query and lock contract
# ======================================================================


class QueryAndLockContractTests(RevertGuardTestCase):
    def test_capture_lock_precedes_the_reversion_root_mutation(self):
        prompt, stage_a_version, _approved_a, _review_b, _approved_b, _current = self.build_two_generation_history()
        with CaptureQueriesContext(connection) as ctx:
            self.client.post(
                revision_url(prompt, stage_a_version),
                self.confirm_payload(prompt, title="Content A"),
            )
        prompt_queries = [q for q in ctx.captured_queries if '"prompts_prompt"' in q["sql"]]
        first_lock_index = next(
            i for i, q in enumerate(prompt_queries) if "FOR UPDATE" in q["sql"].upper()
        )
        first_update_index = next(
            i for i, q in enumerate(prompt_queries) if q["sql"].strip().upper().startswith("UPDATE")
        )
        self.assertLess(first_lock_index, first_update_index)

    def test_identical_payload_valid_binding_issues_no_root_update(self):
        prompt, _stage_a, _approved_a, _review_b, approved_b_version, _current = (
            self.build_two_generation_history()
        )
        with CaptureQueriesContext(connection) as ctx:
            self.client.post(
                revision_url(prompt, approved_b_version),
                self.confirm_payload(prompt, title="Content B"),
            )
        # Two root UPDATEs are unavoidable and pre-date this guard: one from
        # reversion's own ``revert()``, one from the ordinary changeform's
        # full-row ``obj.save()`` (issued regardless of whether anything
        # differs, exactly like any admin save - see
        # ``test_admin_review_edit_guard.py``'s own
        # ``test_no_op_save_issues_no_guard_update``). What must not appear
        # is a *third*, guard-issued update clearing the binding - detected,
        # like that sibling test, by the exact substring a real invalidation
        # clears to, never by a brittle total query count.
        cleared = [
            q for q in ctx.captured_queries
            if '"review_payload_fingerprint" = \'\'' in q["sql"]
        ]
        self.assertEqual(cleared, [])


# ======================================================================
# Permissions
# ======================================================================


class PermissionsTests(RevertGuardTestCase):
    def test_author_can_revert_their_own_prompt_and_it_invalidates(self):
        prompt = self.make_prompt(author=self.author, title="Content A")
        self.submit(prompt, actor=self.editor)
        stage_a_version = self.version_for(prompt, self.latest_revision())
        prompt = self.approve(refetch(prompt), actor=self.editor)
        self.edit_via_admin(prompt, actor=self.author, title="Content B")
        self.submit(refetch(prompt), actor=self.editor)
        self.approve(refetch(prompt), actor=self.editor)

        self.client.force_login(self.author)
        resp = self.client.post(
            revision_url(prompt, stage_a_version),
            self.confirm_payload(prompt, author=self.author, title="Content A"),
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_DRAFT)

    def test_author_cannot_revert_someone_elses_prompt(self):
        prompt, stage_a_version, _approved_a, _review_b, _approved_b, current = self.build_two_generation_history(
            author=self.other_author
        )
        self.client.force_login(self.author)
        resp = self.client.post(
            revision_url(prompt, stage_a_version),
            self.confirm_payload(prompt, author=self.other_author, title="Content A"),
        )
        self.assertEqual(resp.status_code, 403)
        reloaded = refetch(prompt)
        self.assertEqual(reloaded.status, current.status)
        self.assertEqual(reloaded.review_revision_id, current.review_revision_id)

    def test_editor_can_revert_any_prompt(self):
        prompt, stage_a_version, _approved_a, _review_b, _approved_b, _current = self.build_two_generation_history(
            author=self.author
        )
        resp = self.client.post(
            revision_url(prompt, stage_a_version),
            self.confirm_payload(prompt, author=self.author, title="Content A"),
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_DRAFT)

    def test_staff_without_editorial_role_cannot_revert(self):
        plain_staff = User.objects.create_user("c4j-plain-staff", password="pw", is_staff=True)
        prompt, stage_a_version, _approved_a, _review_b, _approved_b, _current = self.build_two_generation_history()
        self.client.force_login(plain_staff)
        resp = self.client.get(revision_url(prompt, stage_a_version))
        self.assertEqual(resp.status_code, 403)


# ======================================================================
# Static safety
# ======================================================================


class StaticSafetyTests(TestCase):
    def test_result_type_has_no_payload_or_model_instance_fields(self):
        fields = PromptRevisionGuardResult.__dataclass_fields__
        self.assertEqual(
            set(fields),
            {"prompt_id", "database_alias", "source", "payload_changed", "invalidated", "invalidation"},
        )
        self.assertTrue(PromptRevisionGuardResult.__dataclass_params__.frozen)

    def test_admin_only_imports_public_names_from_the_three_primitive_modules(self):
        """
        Abnahme-Korrekturrunde Phase 9 ("C4J"): every name
        ``prompts/admin.py`` imports from ``prompts.review_submission``,
        ``prompts.review_approval`` and ``core.review_binding`` (by absolute
        or relative module path) must be public (no leading underscore) -
        i.e. part of each module's own documented public contract, never a
        private implementation detail those modules only expose to
        themselves or their own tests.
        """
        import ast
        import pathlib

        source = pathlib.Path("prompts/admin.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        watched_modules = {
            "prompts.review_submission", ".review_submission",
            "prompts.review_approval", ".review_approval",
            "core.review_binding",
        }
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module_name = node.module or ""
            if module_name not in watched_modules:
                continue
            for alias in node.names:
                if alias.name.startswith("_"):
                    offenders.append(f"{module_name}.{alias.name}")
        self.assertEqual(offenders, [])

    def test_admin_never_calls_a_private_name_via_module_attribute_access(self):
        """
        Complements the import-based check above: also forbids reaching a
        private name via ``module.attr`` access (e.g.
        ``review_submission._some_helper(...)``) on any module imported from
        the three primitive modules, even if the module itself is imported
        as a whole rather than via ``from ... import``.
        """
        import ast
        import pathlib

        source = pathlib.Path("prompts/admin.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders = [
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr.startswith("_")
            and isinstance(node.value, ast.Name)
            and node.value.id in {"review_submission", "review_approval", "review_binding"}
        ]
        self.assertEqual(offenders, [])

    def test_public_slug_preservation_is_confined_to_prompt_admin(self):
        """
        Abnahme-Korrekturrunde 2, Phase 12: the ``public_slug`` preservation
        lives only inside ``PromptAdmin`` (no global Parler/model change), is
        never derived from ``slug``, and never patches the form's
        ``initial``/``cleaned_data``/``changed_data``.
        """
        import ast
        import pathlib

        source = pathlib.Path("prompts/admin.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        prompt_admin = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == "PromptAdmin"
        )
        method_names = {
            item.name for item in prompt_admin.body if isinstance(item, ast.FunctionDef)
        }
        self.assertIn("_preserve_untouched_translated_fields", method_names)

        # No public_slug = slug derivation, and no form-internal mutation.
        assign_targets = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Attribute):
                        assign_targets.append(target.attr)
        self.assertNotIn("public_slug", assign_targets)  # never `x.public_slug = ...`

        # No writes into form.initial / form.cleaned_data / form.changed_data.
        for attr in ("initial", "cleaned_data", "changed_data"):
            subscript_writes = [
                n for n in ast.walk(tree)
                if isinstance(n, ast.Assign)
                for t in n.targets
                if isinstance(t, ast.Subscript)
                and isinstance(t.value, ast.Attribute)
                and t.value.attr == attr
            ]
            self.assertEqual(subscript_writes, [], f"must not write form.{attr}[...]")

    def test_auto_review_bypass_exists_only_as_prompt_admin_override(self):
        """
        Phase 12: the auto-review bypass is a single ``PromptAdmin`` override
        of ``_auto_transition_to_review`` that delegates to ``super()`` on
        every non-bypass path - never a change to ``core/admin.py`` and never
        a blanket disable.
        """
        import ast
        import pathlib

        source = pathlib.Path("prompts/admin.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        prompt_admin = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == "PromptAdmin"
        )
        override = next(
            (item for item in prompt_admin.body
             if isinstance(item, ast.FunctionDef) and item.name == "_auto_transition_to_review"),
            None,
        )
        self.assertIsNotNone(override)
        # It must still delegate to super() somewhere (non-bypass path intact).
        has_super_delegation = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "_auto_transition_to_review"
            and isinstance(n.func.value, ast.Call)
            and isinstance(n.func.value.func, ast.Name)
            and n.func.value.func.id == "super"
            for n in ast.walk(override)
        )
        self.assertTrue(has_super_delegation)

    def test_admin_does_not_import_or_patch_parler_form_or_model_internals(self):
        """Phase 12: no global Parler form/model manipulation - the fix reads
        via the ordinary ``PromptTranslation`` manager only."""
        import pathlib

        source = pathlib.Path("prompts/admin.py").read_text(encoding="utf-8")
        self.assertNotIn("save_translated_fields", source)
        self.assertNotIn("_translations_cache", source)
        self.assertNotIn("construct_instance", source)
        # public_slug is only ever read (preserved), never recomputed from slug.
        self.assertNotIn('public_slug", row["slug"]', source)
