"""
Beta 11.11D2: atomic, fail-closed prompt publish.

``publish_prompt_review()`` is the single production entry point that takes one
Prompt from ``approved`` to ``published``. Everything that has to be true for a
publish to mean anything is checked *immediately before* the status changes,
on a ``SELECT ... FOR UPDATE``-locked row, inside one transaction:

* the actor may publish this object (``content.publish``);
* the row really is ``approved``;
* its review and approval bindings are structurally valid, point at the same
  revision, and that revision really contains this prompt's root version;
* the stored approved fingerprint still matches the canonical Beta 11.11C1/C4D
  v2 payload of what is on disk right now;
* at least one genuinely complete translation exists.

Only then does the FSM transition run, and everything the public site reads -
``live_i18n``, ``live_author``, ``public_slug``, ``is_published``,
``published_at`` - is written inside the same transaction and the same single
reversion revision, with ``last_published_revision_id`` finally pointing at the
root version *of that revision*. Any failure rolls all of it back; there is no
partial publish and no ``published`` row without a complete live projection.
"""
import itertools

import reversion
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import DEFAULT_DB_ALIAS
from django.test import TestCase
from django.utils.translation import override as override_language
from reversion.models import Revision, Version

from catalog.models import Tool
from core.models.editorial import EditorialWorkflowMixin as Workflow
from core.review_binding import (
    fingerprint_review_payload,
    invalidate_editorial_review_state,
)
from prompts.models import Prompt, PromptTranslation
from prompts.review_approval import approve_prompt_review
from prompts.review_payload import build_prompt_review_payload
from prompts.review_publish import (
    PromptReviewPublishError,
    PromptReviewPublishErrorCode,
    PromptReviewPublishResult,
    publish_prompt_review,
)
from prompts.review_submission import submit_prompt_for_review

User = get_user_model()

_counter = itertools.count()

#: Distinguishes "the test did not care who publishes" from an explicit
#: ``actor=None``, which is itself a case under test. A plain ``None`` default
#: would silently swallow the latter.
_DEFAULT_ACTOR = object()


def refetch(prompt):
    """``status`` is a protected FSMField, so ``refresh_from_db()``'s plain
    setattr is rejected - reload through the manager instead."""
    return Prompt.objects.get(pk=prompt.pk)


def translation_of(prompt, language_code):
    return PromptTranslation.objects.get(master_id=prompt.pk, language_code=language_code)


class PublishTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.editor = User.objects.create_user(
            "d2-editor", password="pw", is_staff=True, first_name="Edna", last_name="Editor"
        )
        cls.editor.groups.add(Group.objects.get_or_create(name="Editor")[0])
        cls.author = User.objects.create_user(
            "d2-author", password="pw", is_staff=True, first_name="Aurel", last_name="Author"
        )
        cls.author.groups.add(Group.objects.get_or_create(name="Author")[0])
        cls.other_author = User.objects.create_user(
            "d2-other-author", password="pw", is_staff=True
        )
        cls.other_author.groups.add(Group.objects.get_or_create(name="Author")[0])
        cls.outsider = User.objects.create_user("d2-outsider", password="pw")

    # -- construction --------------------------------------------------

    def make_prompt(self, *, author=None, languages=("en",), title="Titel", body="Inhalt"):
        prompt = Prompt.objects.create(status=Workflow.STATUS_DRAFT, author=author or self.author)
        for lang in languages:
            prompt.create_translation(
                lang,
                title=f"{title} {lang}",
                intro=f"intro {lang}",
                body=f"{body} {lang}",
                outro=f"outro {lang}",
                slug=f"d2-{lang}-{next(_counter)}",
            )
        return prompt

    def approved(self, *, author=None, languages=("en",), **kwargs):
        """A genuinely approved prompt: real C2A submit, real C3A approval by
        someone other than the author (no self-approval)."""
        prompt = self.make_prompt(author=author, languages=languages, **kwargs)
        submit_prompt_for_review(refetch(prompt), actor=self.author)
        approve_prompt_review(refetch(prompt), actor=self.editor)
        approved = refetch(prompt)
        assert approved.status == Workflow.STATUS_APPROVED, approved.status
        return approved

    def back_to_approved(self, prompt):
        """
        Bring an already ``published`` prompt round to ``approved`` again, the
        only way production allows: ``published`` itself is deliberately not
        submittable (C2A's ``_SUBMITTABLE_STATUSES`` is draft/rework), so an
        edit first runs the FSM ``move_to_review`` the admin uses
        (published -> review), then D1's invalidation primitive drops the now
        unbound row to ``draft``, and only then do the real C2A submit and C3A
        approval run. No shortcut, no direct status assignment.
        """
        live = refetch(prompt)
        live.move_to_review(by=self.author)
        live.save()
        invalidate_editorial_review_state(refetch(prompt))
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_DRAFT)
        submit_prompt_for_review(refetch(prompt), actor=self.author)
        approve_prompt_review(refetch(prompt), actor=self.editor)
        approved = refetch(prompt)
        self.assertEqual(approved.status, Workflow.STATUS_APPROVED)
        return approved

    def field_snapshot(self, prompt):
        """Everything a failed publish must leave byte-identical."""
        fresh = refetch(prompt)
        return {
            "status": fresh.status,
            "review_revision_id": fresh.review_revision_id,
            "approved_revision_id": fresh.approved_revision_id,
            "fingerprint": fresh.review_payload_fingerprint,
            "live_i18n": fresh.live_i18n,
            "live_author": fresh.live_author,
            "is_published": fresh.is_published,
            "published_at": fresh.published_at,
            "marker": fresh.last_published_revision_id,
            "public_slugs": {
                t.language_code: t.public_slug
                for t in PromptTranslation.objects.filter(master_id=fresh.pk)
            },
        }

    def assert_unchanged(self, prompt, before, *, revisions_before, versions_before):
        self.assertEqual(self.field_snapshot(prompt), before)
        self.assertEqual(Revision.objects.count(), revisions_before)
        self.assertEqual(Version.objects.count(), versions_before)

    def assert_publish_fails(self, prompt, code, *, actor=_DEFAULT_ACTOR, using=None):
        """Runs the publish, asserts the stable error code, and asserts that
        absolutely nothing moved."""
        before = self.field_snapshot(prompt)
        revisions_before = Revision.objects.count()
        versions_before = Version.objects.count()
        with self.assertRaises(PromptReviewPublishError) as ctx:
            publish_prompt_review(
                refetch(prompt),
                actor=self.author if actor is _DEFAULT_ACTOR else actor,
                using=using,
            )
        self.assertEqual(ctx.exception.code, code)
        self.assert_unchanged(
            prompt, before, revisions_before=revisions_before, versions_before=versions_before
        )
        return ctx.exception

    def root_version_of(self, revision_id, prompt):
        return Version.objects.get(
            revision_id=revision_id,
            content_type__app_label="prompts",
            content_type__model="prompt",
            object_id=str(prompt.pk),
        )


# ======================================================================
# Success contract
# ======================================================================


class PublishSuccessTests(PublishTestCase):
    def test_author_publishes_their_own_approved_prompt(self):
        prompt = self.approved(author=self.author)
        result = publish_prompt_review(refetch(prompt), actor=self.author)

        self.assertIsInstance(result, PromptReviewPublishResult)
        self.assertEqual(result.prompt_id, prompt.pk)
        self.assertEqual(result.database_alias, DEFAULT_DB_ALIAS)
        self.assertEqual(result.previous_status, Workflow.STATUS_APPROVED)
        self.assertEqual(result.current_status, Workflow.STATUS_PUBLISHED)
        self.assertEqual(result.published_language_codes, ("en",))

        after = refetch(prompt)
        self.assertEqual(after.status, Workflow.STATUS_PUBLISHED)
        self.assertTrue(after.is_published)
        self.assertIsNotNone(after.published_at)
        self.assertTrue(after.live_i18n)
        self.assertEqual(after.live_author["display_name"], "Aurel Author")

    def test_editor_publishes_an_approved_prompt(self):
        prompt = self.approved(author=self.author)
        result = publish_prompt_review(refetch(prompt), actor=self.editor)
        self.assertEqual(result.current_status, Workflow.STATUS_PUBLISHED)
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_PUBLISHED)

    def test_english_only_prompt_publishes_english_only(self):
        prompt = self.approved(languages=("en",))
        result = publish_prompt_review(refetch(prompt), actor=self.author)
        self.assertEqual(result.published_language_codes, ("en",))
        self.assertEqual(set(refetch(prompt).live_i18n), {"en"})

    def test_german_only_prompt_publishes_german_only(self):
        prompt = self.approved(languages=("de",))
        result = publish_prompt_review(refetch(prompt), actor=self.author)
        self.assertEqual(result.published_language_codes, ("de",))
        self.assertEqual(set(refetch(prompt).live_i18n), {"de"})

    def test_bilingual_prompt_publishes_both_languages(self):
        prompt = self.approved(languages=("en", "de"))
        result = publish_prompt_review(refetch(prompt), actor=self.author)
        self.assertEqual(result.published_language_codes, ("de", "en"))
        self.assertEqual(set(refetch(prompt).live_i18n), {"en", "de"})

    def test_display_title_is_correct_in_every_published_language(self):
        prompt = self.approved(languages=("en", "de"))
        publish_prompt_review(refetch(prompt), actor=self.author)
        after = refetch(prompt)
        for lang in ("en", "de"):
            with override_language(lang):
                self.assertEqual(after.display_title, f"Titel {lang}")

    def test_language_without_a_translation_stays_invisible(self):
        prompt = self.approved(languages=("en",))
        publish_prompt_review(refetch(prompt), actor=self.author)
        after = refetch(prompt)
        self.assertNotIn("de", after.live_i18n)
        with override_language("de"):
            self.assertFalse(after.display_title)
        self.assertNotIn(
            prompt.pk,
            list(Prompt.objects.visible_in_language("de").values_list("pk", flat=True)),
        )
        self.assertIn(
            prompt.pk,
            list(Prompt.objects.visible_in_language("en").values_list("pk", flat=True)),
        )

    def test_a_stale_caller_object_never_decides_the_outcome(self):
        prompt = self.approved()
        stale = refetch(prompt)
        # The caller's copy still claims "approved" while the database row has
        # already moved on; the locked re-read is the only source of truth.
        Prompt.objects.filter(pk=prompt.pk).update(status=Workflow.STATUS_DRAFT)
        self.assertEqual(stale.status, Workflow.STATUS_APPROVED)

        with self.assertRaises(PromptReviewPublishError) as ctx:
            publish_prompt_review(stale, actor=self.author)
        self.assertEqual(
            ctx.exception.code, PromptReviewPublishErrorCode.STATUS_NOT_PUBLISHABLE
        )

    def test_exactly_one_revision_with_one_root_version(self):
        prompt = self.approved(languages=("en", "de"))
        revisions_before = Revision.objects.count()

        result = publish_prompt_review(refetch(prompt), actor=self.author)

        self.assertEqual(Revision.objects.count(), revisions_before + 1)
        revision = Revision.objects.get(pk=result.revision_id)
        roots = Version.objects.filter(
            revision=revision,
            content_type__app_label="prompts",
            content_type__model="prompt",
            object_id=str(prompt.pk),
        )
        self.assertEqual(roots.count(), 1)
        self.assertEqual(roots.get().pk, result.root_version_id)

    def test_translation_versions_follow_the_reversion_graph(self):
        prompt = self.approved(languages=("en", "de"))
        result = publish_prompt_review(refetch(prompt), actor=self.author)
        translation_versions = Version.objects.filter(
            revision_id=result.revision_id,
            content_type__app_label="prompts",
            content_type__model="prompttranslation",
        )
        self.assertEqual(translation_versions.count(), 2)

    def test_actor_and_comment_are_recorded(self):
        prompt = self.approved()
        result = publish_prompt_review(refetch(prompt), actor=self.editor)
        revision = Revision.objects.get(pk=result.revision_id)
        self.assertEqual(revision.user_id, self.editor.pk)
        self.assertTrue(revision.comment)
        self.assertIn("publish", revision.comment.lower())

    def test_marker_points_at_the_new_root_version(self):
        prompt = self.approved()
        approval_versions = set(
            Version.objects.filter(
                content_type__app_label="prompts",
                content_type__model="prompt",
                object_id=str(prompt.pk),
            ).values_list("pk", flat=True)
        )

        result = publish_prompt_review(refetch(prompt), actor=self.author)
        after = refetch(prompt)

        self.assertEqual(after.last_published_revision_id, result.root_version_id)
        # never an older root, the approval version, or the review version
        self.assertNotIn(after.last_published_revision_id, approval_versions)
        marker_version = Version.objects.get(pk=after.last_published_revision_id)
        self.assertEqual(marker_version.revision_id, result.revision_id)
        self.assertEqual(marker_version.object_id, str(prompt.pk))
        self.assertEqual(marker_version.content_type.model, "prompt")

    def test_marker_is_never_a_translation_or_foreign_version(self):
        other = self.approved()
        publish_prompt_review(refetch(other), actor=self.author)
        prompt = self.approved()
        result = publish_prompt_review(refetch(prompt), actor=self.author)

        marker_version = Version.objects.get(
            pk=refetch(prompt).last_published_revision_id
        )
        self.assertNotEqual(marker_version.content_type.model, "prompttranslation")
        self.assertEqual(marker_version.object_id, str(prompt.pk))
        self.assertNotEqual(marker_version.object_id, str(other.pk))
        self.assertEqual(marker_version.pk, result.root_version_id)

    def test_binding_and_fingerprint_survive_the_publish(self):
        prompt = self.approved()
        before = refetch(prompt)
        publish_prompt_review(refetch(prompt), actor=self.author)
        after = refetch(prompt)
        self.assertEqual(after.review_revision_id, before.review_revision_id)
        self.assertEqual(after.approved_revision_id, before.approved_revision_id)
        self.assertEqual(
            after.review_payload_fingerprint, before.review_payload_fingerprint
        )


# ======================================================================
# Public slug contract
# ======================================================================


class PublicSlugContractTests(PublishTestCase):
    """
    The existing, code-defined policy (``Prompt.on_after_publish()``) is
    "``public_slug`` mirrors ``slug``" - including on a republish after the
    draft slug changed. D2 keeps that policy exactly and only fixes *when* it
    runs: before the live snapshot is built, so the persisted translation and
    ``live_i18n`` can no longer disagree.
    """

    def assert_slug_consistency(self, prompt, languages):
        after = refetch(prompt)
        for lang in languages:
            translation = translation_of(after, lang)
            self.assertEqual(
                translation.public_slug,
                after.live_i18n[lang]["public_slug"],
                f"persisted public_slug and live_i18n disagree for {lang!r}",
            )
            self.assertEqual(translation.public_slug, translation.slug)

    def test_first_publish_sets_public_slug_and_snapshots_it(self):
        prompt = self.approved(languages=("en",))
        self.assertIsNone(translation_of(prompt, "en").public_slug)

        publish_prompt_review(refetch(prompt), actor=self.author)

        after = refetch(prompt)
        self.assertIsNotNone(translation_of(after, "en").public_slug)
        self.assertIsNotNone(after.live_i18n["en"]["public_slug"])
        self.assert_slug_consistency(prompt, ("en",))

    def test_republish_without_a_slug_change_keeps_everything_stable(self):
        prompt = self.approved(languages=("en",))
        publish_prompt_review(refetch(prompt), actor=self.author)
        first = translation_of(refetch(prompt), "en").public_slug

        # a real second round through the workflow without touching the slug
        self.back_to_approved(prompt)
        publish_prompt_review(refetch(prompt), actor=self.author)

        self.assertEqual(translation_of(refetch(prompt), "en").public_slug, first)
        self.assert_slug_consistency(prompt, ("en",))

    def test_republish_after_a_slug_change_publishes_the_new_slug(self):
        prompt = self.approved(languages=("en",))
        publish_prompt_review(refetch(prompt), actor=self.author)
        old = translation_of(refetch(prompt), "en").public_slug

        new_slug = f"d2-changed-{next(_counter)}"
        PromptTranslation.objects.filter(master_id=prompt.pk, language_code="en").update(
            slug=new_slug
        )
        self.back_to_approved(prompt)
        publish_prompt_review(refetch(prompt), actor=self.author)

        after = refetch(prompt)
        self.assertEqual(translation_of(after, "en").public_slug, new_slug)
        self.assertNotEqual(translation_of(after, "en").public_slug, old)
        self.assert_slug_consistency(prompt, ("en",))

    def test_two_languages_keep_their_own_slugs(self):
        prompt = self.approved(languages=("en", "de"))
        publish_prompt_review(refetch(prompt), actor=self.author)
        after = refetch(prompt)
        self.assertNotEqual(
            after.live_i18n["en"]["public_slug"], after.live_i18n["de"]["public_slug"]
        )
        self.assert_slug_consistency(prompt, ("en", "de"))

    def test_missing_second_language_is_absent_from_the_snapshot(self):
        prompt = self.approved(languages=("en",))
        publish_prompt_review(refetch(prompt), actor=self.author)
        self.assertEqual(set(refetch(prompt).live_i18n), {"en"})


# ======================================================================
# Fail-closed contract
# ======================================================================


class PublishStatusGateTests(PublishTestCase):
    def test_draft_cannot_be_published(self):
        prompt = self.make_prompt()
        self.assert_publish_fails(
            prompt, PromptReviewPublishErrorCode.STATUS_NOT_PUBLISHABLE
        )

    def test_review_cannot_be_published(self):
        prompt = self.make_prompt()
        submit_prompt_for_review(refetch(prompt), actor=self.author)
        self.assert_publish_fails(
            prompt, PromptReviewPublishErrorCode.STATUS_NOT_PUBLISHABLE
        )

    def test_already_published_cannot_be_published_again(self):
        prompt = self.approved()
        publish_prompt_review(refetch(prompt), actor=self.author)
        self.assert_publish_fails(
            prompt, PromptReviewPublishErrorCode.STATUS_NOT_PUBLISHABLE
        )

    def test_rework_cannot_be_published(self):
        prompt = self.make_prompt()
        submit_prompt_for_review(refetch(prompt), actor=self.author)
        locked = refetch(prompt)
        locked.request_rework(by=self.editor, note="please fix")
        locked.save()
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_REWORK)
        self.assert_publish_fails(
            prompt, PromptReviewPublishErrorCode.STATUS_NOT_PUBLISHABLE
        )


class PublishPermissionTests(PublishTestCase):
    def test_missing_actor_is_rejected(self):
        prompt = self.approved()
        self.assert_publish_fails(
            prompt, PromptReviewPublishErrorCode.INVALID_ACTOR, actor=None
        )

    def test_actor_without_publish_permission_is_rejected(self):
        prompt = self.approved()
        self.assert_publish_fails(
            prompt, PromptReviewPublishErrorCode.PERMISSION_DENIED, actor=self.outsider
        )

    def test_author_cannot_publish_someone_elses_prompt(self):
        prompt = self.approved(author=self.author)
        self.assert_publish_fails(
            prompt,
            PromptReviewPublishErrorCode.PERMISSION_DENIED,
            actor=self.other_author,
        )


class PublishBindingTests(PublishTestCase):
    def test_missing_review_revision_is_rejected(self):
        prompt = self.approved()
        Prompt.objects.filter(pk=prompt.pk).update(review_revision=None)
        self.assert_publish_fails(
            prompt, PromptReviewPublishErrorCode.REVIEW_BINDING_INVALID
        )

    def test_missing_approved_revision_is_rejected(self):
        prompt = self.approved()
        Prompt.objects.filter(pk=prompt.pk).update(approved_revision=None)
        self.assert_publish_fails(
            prompt, PromptReviewPublishErrorCode.APPROVED_BINDING_INVALID
        )

    def test_diverging_review_and_approved_revisions_are_rejected(self):
        other = self.approved()
        prompt = self.approved()
        Prompt.objects.filter(pk=prompt.pk).update(
            approved_revision_id=other.review_revision_id
        )
        self.assert_publish_fails(
            prompt, PromptReviewPublishErrorCode.APPROVED_BINDING_INVALID
        )

    def test_a_foreign_revision_is_rejected(self):
        other = self.approved()
        prompt = self.approved()
        Prompt.objects.filter(pk=prompt.pk).update(
            review_revision_id=other.review_revision_id,
            approved_revision_id=other.review_revision_id,
        )
        self.assert_publish_fails(
            prompt, PromptReviewPublishErrorCode.REVIEW_BINDING_INVALID
        )

    def test_a_revision_not_containing_the_root_is_rejected(self):
        prompt = self.approved()
        empty = Revision.objects.create(
            date_created=refetch(prompt).created_at, comment="empty"
        )
        Prompt.objects.filter(pk=prompt.pk).update(
            review_revision=empty, approved_revision=empty
        )
        self.assert_publish_fails(
            prompt, PromptReviewPublishErrorCode.REVIEW_BINDING_INVALID
        )

    def test_an_empty_fingerprint_is_rejected(self):
        prompt = self.approved()
        Prompt.objects.filter(pk=prompt.pk).update(review_payload_fingerprint="")
        self.assert_publish_fails(
            prompt, PromptReviewPublishErrorCode.REVIEW_BINDING_INVALID
        )

    def test_a_malformed_fingerprint_is_rejected(self):
        prompt = self.approved()
        Prompt.objects.filter(pk=prompt.pk).update(
            review_payload_fingerprint="NOT-A-SHA256"
        )
        self.assert_publish_fails(
            prompt, PromptReviewPublishErrorCode.REVIEW_BINDING_INVALID
        )


class PublishStaleFingerprintTests(PublishTestCase):
    """Every payload-relevant mutation between approval and publish must be
    caught by the fingerprint re-check, never silently published."""

    def _assert_stale(self, prompt, *, actor=_DEFAULT_ACTOR):
        self.assert_publish_fails(
            prompt, PromptReviewPublishErrorCode.REVIEW_PAYLOAD_CHANGED, actor=actor
        )

    def test_changed_title_is_caught(self):
        prompt = self.approved()
        PromptTranslation.objects.filter(master_id=prompt.pk).update(title="Changed")
        self._assert_stale(prompt)

    def test_changed_body_is_caught(self):
        prompt = self.approved()
        PromptTranslation.objects.filter(master_id=prompt.pk).update(body="Changed body")
        self._assert_stale(prompt)

    def test_changed_slug_is_caught(self):
        prompt = self.approved()
        PromptTranslation.objects.filter(master_id=prompt.pk).update(
            slug=f"d2-stale-{next(_counter)}"
        )
        self._assert_stale(prompt)

    def test_changed_author_fk_is_caught(self):
        prompt = self.approved(author=self.author)
        Prompt.objects.filter(pk=prompt.pk).update(author=self.other_author)
        # Published by the editor on purpose: reassigning the author strips
        # `self.author`'s own `content.publish` right, so publishing as them
        # would stop at the permission gate and never reach the fingerprint
        # re-check this test is about.
        self._assert_stale(prompt, actor=self.editor)

    def test_changed_tool_membership_is_caught(self):
        prompt = self.approved()
        tool = Tool.objects.create(slug=f"d2-tool-{next(_counter)}")
        tool.create_translation("en", name="D2 Tool")
        refetch(prompt).tools.add(tool)
        self._assert_stale(prompt)

    def test_changed_tag_membership_is_caught(self):
        prompt = self.approved()
        refetch(prompt).tags.add("d2-new-tag")
        self._assert_stale(prompt)

    def test_an_author_display_name_change_is_not_payload_relevant(self):
        """Payload v2 (Beta 11.11C4D) carries only the author *id*, so a
        renamed account must not block a publish."""
        prompt = self.approved(author=self.author)
        User.objects.filter(pk=self.author.pk).update(first_name="Renamed")
        result = publish_prompt_review(refetch(prompt), actor=self.author)
        self.assertEqual(result.current_status, Workflow.STATUS_PUBLISHED)


class PublishTranslationCompletenessTests(PublishTestCase):
    def test_a_prompt_without_any_translation_is_rejected(self):
        prompt = Prompt.objects.create(
            status=Workflow.STATUS_DRAFT, author=self.author
        )
        # Reach `approved` without ever creating a translation.
        Prompt.objects.filter(pk=prompt.pk).update(status=Workflow.STATUS_APPROVED)
        with self.assertRaises(PromptReviewPublishError) as ctx:
            publish_prompt_review(refetch(prompt), actor=self.author)
        self.assertIn(
            ctx.exception.code,
            (
                PromptReviewPublishErrorCode.NO_PUBLISHABLE_TRANSLATION,
                PromptReviewPublishErrorCode.REVIEW_BINDING_INVALID,
            ),
        )
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_APPROVED)

    def test_an_incomplete_translation_is_rejected(self):
        prompt = self.approved(languages=("en",))
        # Blank the title *after* approval, then re-approve the new payload so
        # the fingerprint matches and only completeness can fail.
        PromptTranslation.objects.filter(master_id=prompt.pk).update(title="   ")
        payload = build_prompt_review_payload(refetch(prompt))
        Prompt.objects.filter(pk=prompt.pk).update(
            review_payload_fingerprint=fingerprint_review_payload(payload)
        )
        self.assert_publish_fails(
            prompt, PromptReviewPublishErrorCode.INCOMPLETE_TRANSLATION
        )


class PublishInfrastructureFailureTests(PublishTestCase):
    """Infrastructure-level failures must roll the whole publish back rather
    than leaving a half-published row."""

    def test_a_reversion_capture_failure_rolls_everything_back(self):
        from unittest import mock

        prompt = self.approved()
        before = self.field_snapshot(prompt)
        revisions_before = Revision.objects.count()
        versions_before = Version.objects.count()

        with mock.patch(
            "prompts.review_publish._resolve_root_version",
            side_effect=RuntimeError("root version boom"),
        ), self.assertRaises(RuntimeError):
            publish_prompt_review(refetch(prompt), actor=self.author)

        self.assert_unchanged(
            prompt, before, revisions_before=revisions_before, versions_before=versions_before
        )

    def test_a_marker_update_failure_rolls_everything_back(self):
        from unittest import mock

        prompt = self.approved()
        before = self.field_snapshot(prompt)
        revisions_before = Revision.objects.count()
        versions_before = Version.objects.count()

        with mock.patch(
            "prompts.review_publish._store_publish_marker",
            side_effect=RuntimeError("marker boom"),
        ), self.assertRaises(RuntimeError):
            publish_prompt_review(refetch(prompt), actor=self.author)

        self.assert_unchanged(
            prompt, before, revisions_before=revisions_before, versions_before=versions_before
        )

    def test_a_postcondition_failure_rolls_everything_back(self):
        from unittest import mock

        prompt = self.approved()
        before = self.field_snapshot(prompt)
        revisions_before = Revision.objects.count()
        versions_before = Version.objects.count()

        with mock.patch(
            "prompts.review_publish._verify_publish_postconditions",
            side_effect=PromptReviewPublishError(
                PromptReviewPublishErrorCode.PUBLISH_POSTCONDITION_FAILED, "forced"
            ),
        ), self.assertRaises(PromptReviewPublishError) as ctx:
            publish_prompt_review(refetch(prompt), actor=self.author)
        self.assertEqual(
            ctx.exception.code, PromptReviewPublishErrorCode.PUBLISH_POSTCONDITION_FAILED
        )
        self.assert_unchanged(
            prompt, before, revisions_before=revisions_before, versions_before=versions_before
        )

    def test_a_revision_that_was_never_captured_is_reported(self):
        """The capture is call-local by design (C2A's ContextVar + dispatch_uid
        pattern), so "the signal never fired for us" must be a hard, named
        error - never a fallback to a "newest revision" guess that a
        concurrent commit could win."""
        from unittest import mock

        prompt = self.approved()
        before = self.field_snapshot(prompt)

        with mock.patch("prompts.review_publish.post_revision_commit.connect"), self.assertRaises(PromptReviewPublishError) as ctx:
            publish_prompt_review(refetch(prompt), actor=self.author)
        self.assertEqual(
            ctx.exception.code, PromptReviewPublishErrorCode.REVISION_NOT_CAPTURED
        )
        self.assertEqual(self.field_snapshot(prompt), before)

    def test_more_than_one_captured_revision_is_reported(self):
        from unittest import mock

        prompt = self.approved()
        before = self.field_snapshot(prompt)

        with mock.patch(
            "prompts.review_publish._capture_publish_revision",
            side_effect=PromptReviewPublishError(
                PromptReviewPublishErrorCode.MULTIPLE_REVISIONS_CAPTURED, "forced"
            ),
        ), self.assertRaises(PromptReviewPublishError) as ctx:
            publish_prompt_review(refetch(prompt), actor=self.author)
        self.assertEqual(
            ctx.exception.code,
            PromptReviewPublishErrorCode.MULTIPLE_REVISIONS_CAPTURED,
        )
        self.assertEqual(self.field_snapshot(prompt), before)

    def test_a_revision_without_a_root_version_is_reported(self):
        """``_resolve_root_version`` looks the version up by an exact
        (revision, content_type, object_id, db) tuple; if that finds nothing
        the marker cannot be written and the publish must roll back rather
        than fall back to some other version."""
        from unittest import mock

        prompt = self.approved()
        before = self.field_snapshot(prompt)
        empty = Revision.objects.create(
            date_created=refetch(prompt).created_at, comment="no versions"
        )

        with mock.patch(
            "prompts.review_publish._capture_publish_revision", return_value=empty
        ), self.assertRaises(PromptReviewPublishError) as ctx:
            publish_prompt_review(refetch(prompt), actor=self.author)
        self.assertEqual(
            ctx.exception.code, PromptReviewPublishErrorCode.ROOT_VERSION_MISSING
        )
        self.assertEqual(self.field_snapshot(prompt), before)

    def test_an_ambiguous_root_version_is_reported(self):
        """``reversion.Version`` has a unique_together on exactly this tuple,
        so two rows are structurally impossible today. The check exists so a
        future schema change could not silently turn that into a wrong
        marker - which is why it is proven by forcing the branch rather than
        by violating the constraint."""
        from unittest import mock

        prompt = self.approved()
        before = self.field_snapshot(prompt)

        with mock.patch(
            "prompts.review_publish._resolve_root_version",
            side_effect=PromptReviewPublishError(
                PromptReviewPublishErrorCode.ROOT_VERSION_AMBIGUOUS, "forced"
            ),
        ), self.assertRaises(PromptReviewPublishError) as ctx:
            publish_prompt_review(refetch(prompt), actor=self.author)
        self.assertEqual(
            ctx.exception.code, PromptReviewPublishErrorCode.ROOT_VERSION_AMBIGUOUS
        )
        self.assertEqual(self.field_snapshot(prompt), before)

    def test_an_already_open_reversion_context_is_rejected(self):
        prompt = self.approved()
        before = self.field_snapshot(prompt)
        with reversion.create_revision(), self.assertRaises(PromptReviewPublishError) as ctx:
            publish_prompt_review(refetch(prompt), actor=self.author)
        self.assertEqual(
            ctx.exception.code, PromptReviewPublishErrorCode.ACTIVE_REVERSION_CONTEXT
        )
        self.assertEqual(self.field_snapshot(prompt), before)


class PublishAliasTests(PublishTestCase):
    def test_an_unknown_alias_is_rejected(self):
        prompt = self.approved()
        self.assert_publish_fails(
            prompt,
            PromptReviewPublishErrorCode.INVALID_DATABASE_ALIAS,
            using="not-a-real-alias",
        )

    def test_a_non_prompt_object_is_rejected(self):
        with self.assertRaises(PromptReviewPublishError) as ctx:
            publish_prompt_review(None, actor=self.author)
        self.assertEqual(
            ctx.exception.code, PromptReviewPublishErrorCode.UNSUPPORTED_OBJECT
        )

    def test_an_unsaved_prompt_is_rejected(self):
        with self.assertRaises(PromptReviewPublishError) as ctx:
            publish_prompt_review(Prompt(), actor=self.author)
        self.assertEqual(ctx.exception.code, PromptReviewPublishErrorCode.UNSAVED_OBJECT)

    def test_a_non_string_alias_raises_type_error(self):
        prompt = self.approved()
        with self.assertRaises(TypeError):
            publish_prompt_review(refetch(prompt), actor=self.author, using=object())

    def test_an_explicit_alias_contradicting_the_objects_own_is_rejected(self):
        """Fail-closed rather than a silent cross-database publish. Built by
        hand because a second alias is not configured in tests - what matters
        is that the mismatch is refused, not which two aliases they are."""
        prompt = self.approved()
        stale = refetch(prompt)
        stale._state.db = "other"
        with self.assertRaises(PromptReviewPublishError) as ctx:
            publish_prompt_review(stale, actor=self.author, using=DEFAULT_DB_ALIAS)
        self.assertEqual(
            ctx.exception.code, PromptReviewPublishErrorCode.DATABASE_ALIAS_MISMATCH
        )

    def test_an_unknown_alias_is_reported_before_any_mismatch(self):
        """Ordering contract shared with C1/C2A/C3A: an alias that does not
        exist is always INVALID_DATABASE_ALIAS, never disguised as a mere
        conflict with the object's own alias."""
        prompt = self.approved()
        stale = refetch(prompt)
        stale._state.db = "other"
        with self.assertRaises(PromptReviewPublishError) as ctx:
            publish_prompt_review(stale, actor=self.author, using="not-a-real-alias")
        self.assertEqual(
            ctx.exception.code, PromptReviewPublishErrorCode.INVALID_DATABASE_ALIAS
        )

    def test_an_actor_on_a_contradicting_alias_is_rejected(self):
        prompt = self.approved()
        actor = User.objects.get(pk=self.author.pk)
        actor._state.db = "other"
        self.assert_publish_fails(
            prompt,
            PromptReviewPublishErrorCode.ACTOR_DATABASE_ALIAS_MISMATCH,
            actor=actor,
        )

    def test_a_deleted_prompt_is_reported_as_missing(self):
        prompt = self.approved()
        stale = refetch(prompt)
        Prompt.objects.filter(pk=prompt.pk).delete()
        with self.assertRaises(PromptReviewPublishError) as ctx:
            publish_prompt_review(stale, actor=self.author)
        self.assertEqual(
            ctx.exception.code, PromptReviewPublishErrorCode.OBJECT_NOT_FOUND
        )
