"""
Beta 11.11D3C: a reversion rollback restores the *selected revision*, workflow
and publication state included.

A rollback is neither a normal edit nor a forbidden hard-delete path. It is a
deliberate "make the object be exactly what it was" action, so the status,
``is_published``, the live snapshots, the public slugs and the review/approval
binding stored in the target revision must survive it. Rows added *after* that
revision (translations, sections, items, entries) are removed by
``Revision.revert(delete=True)`` - that is the intended exact-rollback
behaviour, not a defect, and it is asserted here as such.

What this module does **not** relax: the first ordinary edit *after* the
rollback goes straight back through the existing D1/C4G/C4H invalidation
contract.

Everything is driven through the real admin URLs - the changelist workflow
actions to reach genuine historical states, and
``admin:<app>_<model>_revision`` to roll back - never by writing ``status``
directly.
"""
import itertools
import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import translation
from reversion.models import Revision, Version

from catalog.models import Tool
from compare.models import Comparison, ComparisonToolEntry, ComparisonTranslation
from core.models.editorial import EditorialWorkflowMixin as Workflow
from core.review_binding import validate_review_binding
from guides.models import Guide, GuideItem, GuideSection, GuideTranslation
from prompts.models import Prompt, PromptTranslation
from usecases.models import UseCase, UseCaseTranslation

User = get_user_model()

_counter = itertools.count()


def _unique(prefix):
    return f"{prefix}-{next(_counter)}"


def refetch(model, pk):
    """django-fsm's protected ``status`` descriptor rejects the ``setattr``
    ``refresh_from_db()`` performs, so reload through the manager."""
    return model.objects.get(pk=pk)


def changelist_url(prefix):
    return reverse(f"admin:{prefix}_changelist")


def change_url(prefix, pk):
    return reverse(f"admin:{prefix}_change", args=[pk])


def revision_url(prefix, pk, version_id):
    return reverse(f"admin:{prefix}_revision", args=[pk, version_id])


class ExactReversionTestCase(TestCase):
    """Shared users plus the workflow/rollback plumbing every type needs."""

    @classmethod
    def setUpTestData(cls):
        cls.editor = User.objects.create_user("d3c-editor", password="pw", is_staff=True)
        cls.editor.groups.add(Group.objects.get_or_create(name="Editor")[0])
        cls.author = User.objects.create_user("d3c-author", password="pw", is_staff=True)
        cls.author.groups.add(Group.objects.get_or_create(name="Author")[0])
        cls.superuser = User.objects.create_superuser(
            "d3c-su", "d3c-su@example.com", "pw"
        )

    def setUp(self):
        self.client.force_login(self.superuser)

    # -- workflow ------------------------------------------------------

    def run_action(self, prefix, action, pk):
        resp = self.client.post(
            changelist_url(prefix),
            data={"action": action, "_selected_action": [str(pk)], "index": "0"},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)

    def drive_to(self, prefix, model, pk, target_status):
        """Walk the real changelist actions until ``target_status`` is reached."""
        steps = {
            Workflow.STATUS_REVIEW: ["action_submit_for_review"],
            Workflow.STATUS_APPROVED: ["action_submit_for_review", "action_approve"],
            Workflow.STATUS_PUBLISHED: [
                "action_submit_for_review",
                "action_approve",
                "action_publish",
            ],
            Workflow.STATUS_ARCHIVED: ["action_archive"],
            Workflow.STATUS_DRAFT: [],
        }[target_status]
        for action in steps:
            self.run_action(prefix, action, pk)
        obj = refetch(model, pk)
        self.assertEqual(obj.status, target_status)
        return obj

    def latest_version_for(self, model, pk):
        revision = Revision.objects.latest("pk")
        meta = model._meta
        return Version.objects.get(
            revision=revision,
            object_id=str(pk),
            content_type__app_label=meta.app_label,
            content_type__model=meta.model_name,
        )

    # -- the selected revision is the source of truth --------------------

    def target_fields(self, version):
        """The root row exactly as django-reversion serialized it."""
        return json.loads(version.serialized_data)[0]["fields"]

    def target_translation_fields(self, version, translation_model, language="en"):
        meta = translation_model._meta
        for child in version.revision.version_set.filter(
            content_type__app_label=meta.app_label,
            content_type__model=meta.model_name,
        ):
            fields = json.loads(child.serialized_data)[0]["fields"]
            if fields.get("language_code") == language:
                return fields
        return {}

    #: Root fields every editorial type stores and a rollback must restore.
    SHARED_ROOT_FIELDS = (
        ("status", "status"),
        ("is_published", "is_published"),
        ("live_i18n", "live_i18n"),
        ("review_revision_id", "review_revision"),
        ("approved_revision_id", "approved_revision"),
        ("review_payload_fingerprint", "review_payload_fingerprint"),
        ("reviewed_by_id", "reviewed_by"),
        ("author_id", "author"),
    )

    def assert_matches_target_revision(
        self, model, pk, version, *, translation_model, extra_fields=()
    ):
        """Compare the row after the rollback against the *serialized target
        revision* - never against the live row as it happened to look when
        that revision was written, which can differ (see
        ``PromptSubmitRevisionBindingTests``)."""
        target = self.target_fields(version)
        obj = refetch(model, pk)

        for attr, serialized in tuple(self.SHARED_ROOT_FIELDS) + tuple(extra_fields):
            with self.subTest(field=attr):
                self.assertEqual(getattr(obj, attr), target[serialized])

        translated = self.target_translation_fields(version, translation_model)
        for name in ("title", "slug", "public_slug"):
            if name in translated:
                with self.subTest(field=f"translation.{name}"):
                    self.assertEqual(
                        obj.safe_translation_getter(
                            name, language_code="en", any_language=False
                        ),
                        translated[name],
                    )

    def rollback(self, prefix, pk, version, payload):
        resp = self.client.post(revision_url(prefix, pk, version.pk), data=payload)
        self.assertEqual(
            resp.status_code, 302, getattr(resp, "content", b"")[:2000]
        )
        return resp


# ======================================================================
# Per-type builders and changeform payloads
# ======================================================================


class PromptExactReversionTests(ExactReversionTestCase):
    prefix = "prompts_prompt"
    model = Prompt

    def make(self, *, title="Content A"):
        with translation.override("en"):
            obj = Prompt.objects.create(author=self.editor, status=Workflow.STATUS_DRAFT)
            obj.create_translation(
                "en", title=title, intro="intro", body="body", outro="outro",
                slug=_unique("d3c-prompt"),
            )
        return obj

    def payload(self, obj, *, title=None):
        return {
            "author": str(obj.author_id or ""),
            "review_note": obj.review_note,
            "published_at_0": "",
            "published_at_1": "",
            "tools": [],
            "slug": obj.safe_translation_getter("slug", language_code="en"),
            "title": title if title is not None else obj.safe_translation_getter(
                "title", language_code="en"
            ),
            "intro": "intro",
            "body": "body",
            "outro": "outro",
            "_continue": "Save",
        }

    def assert_prompt_matches(self, pk, version):
        self.assert_matches_target_revision(
            Prompt, pk, version,
            translation_model=PromptTranslation,
            extra_fields=(("live_author", "live_author"),),
        )

    def test_rollback_to_published_stays_published(self):
        obj = self.make()
        published = self.drive_to(self.prefix, Prompt, obj.pk, Workflow.STATUS_PUBLISHED)
        version = self.latest_version_for(Prompt, obj.pk)
        target = self.target_fields(version)
        self.assertEqual(target["status"], Workflow.STATUS_PUBLISHED)
        self.assertTrue(target["is_published"])
        self.assertTrue(target["live_i18n"])
        self.assertIsNotNone(target["live_author"])

        # Move the working state away from the target revision.
        self.client.post(change_url(self.prefix, obj.pk), self.payload(published, title="Content B"))
        self.assertEqual(refetch(Prompt, obj.pk).status, Workflow.STATUS_DRAFT)

        self.rollback(self.prefix, obj.pk, version, self.payload(published))
        self.assert_prompt_matches(obj.pk, version)

    def test_rollback_to_approved_stays_approved(self):
        obj = self.make()
        approved = self.drive_to(self.prefix, Prompt, obj.pk, Workflow.STATUS_APPROVED)
        version = self.latest_version_for(Prompt, obj.pk)
        target = self.target_fields(version)
        self.assertEqual(target["status"], Workflow.STATUS_APPROVED)
        # C3A binds approved_revision inside the revision it captures, so this
        # snapshot carries a provable binding - unlike a submit snapshot.
        self.assertIsNotNone(target["approved_revision"])

        self.client.post(change_url(self.prefix, obj.pk), self.payload(approved, title="Content B"))
        self.rollback(self.prefix, obj.pk, version, self.payload(approved))
        self.assert_prompt_matches(obj.pk, version)

    def test_rollback_to_archived_stays_archived(self):
        obj = self.make()
        self.drive_to(self.prefix, Prompt, obj.pk, Workflow.STATUS_PUBLISHED)
        archived = self.drive_to(self.prefix, Prompt, obj.pk, Workflow.STATUS_ARCHIVED)
        version = self.latest_version_for(Prompt, obj.pk)
        target = self.target_fields(version)
        self.assertEqual(target["status"], Workflow.STATUS_ARCHIVED)
        self.assertFalse(target["is_published"])

        self.run_action(self.prefix, "action_restore_draft", obj.pk)
        self.assertEqual(refetch(Prompt, obj.pk).status, Workflow.STATUS_DRAFT)

        self.rollback(self.prefix, obj.pk, version, self.payload(archived))
        self.assert_prompt_matches(obj.pk, version)

    def test_later_translation_is_removed_by_the_rollback(self):
        obj = self.make()
        published = self.drive_to(self.prefix, Prompt, obj.pk, Workflow.STATUS_PUBLISHED)
        version = self.latest_version_for(Prompt, obj.pk)

        with translation.override("en"):
            refetch(Prompt, obj.pk).create_translation(
                "de", title="DE", intro="i", body="b", outro="o", slug=_unique("d3c-de")
            )
        self.assertEqual(
            sorted(refetch(Prompt, obj.pk).get_available_languages()), ["de", "en"]
        )

        self.rollback(self.prefix, obj.pk, version, self.payload(published))

        after = refetch(Prompt, obj.pk)
        self.assertEqual(list(after.get_available_languages()), ["en"])
        self.assertEqual(after.status, Workflow.STATUS_PUBLISHED)

    def test_a_normal_edit_after_the_rollback_still_invalidates(self):
        obj = self.make()
        published = self.drive_to(self.prefix, Prompt, obj.pk, Workflow.STATUS_PUBLISHED)
        version = self.latest_version_for(Prompt, obj.pk)

        self.client.post(change_url(self.prefix, obj.pk), self.payload(published, title="Content B"))
        self.rollback(self.prefix, obj.pk, version, self.payload(published))
        self.assertEqual(refetch(Prompt, obj.pk).status, Workflow.STATUS_PUBLISHED)

        restored = refetch(Prompt, obj.pk)
        resp = self.client.post(
            change_url(self.prefix, obj.pk), self.payload(restored, title="Content C")
        )
        self.assertEqual(resp.status_code, 302)
        after = refetch(Prompt, obj.pk)
        self.assertNotEqual(after.status, Workflow.STATUS_PUBLISHED)
        self.assertEqual(
            after.safe_translation_getter("title", language_code="en"), "Content C"
        )


class PromptSubmitRevisionBindingTests(ExactReversionTestCase):
    """Where "the selected revision is the source of truth" and Beta 11.11C4J's
    fail-closed binding rule meet.

    Beta 11.11C2A deliberately does *not* bind ``review_revision`` inside the
    revision it captures (see ``prompts/review_submission.py``: the root
    version in that revision is intentionally not self-referential, because
    the revision's own id does not exist yet while it is being written). A
    submit snapshot therefore stores ``review_revision = None`` even though
    the live row was properly bound a moment later.

    Beta 11.11D3C: that purely technical gap must not collapse an otherwise
    exact rollback to ``draft``. The missing value *is* the selected revision,
    and both facts needed to prove it - the revision contains this prompt's
    root version, and the restored fingerprint still describes the restored
    content - are checkable with the central primitives. So the binding is
    reconstructed and the historical ``review`` stands.

    The fail-closed rule itself is untouched: everything that cannot be proven
    still lands in ``draft`` (see
    ``prompts/tests/test_admin_revision_revert_guard.py``'s
    ``RevertReviewBindingFailClosedTests``).
    """

    prefix = "prompts_prompt"

    def make(self):
        with translation.override("en"):
            obj = Prompt.objects.create(author=self.editor, status=Workflow.STATUS_DRAFT)
            obj.create_translation(
                "en", title="Content A", intro="intro", body="body", outro="outro",
                slug=_unique("d3c-bind"),
            )
        return obj

    def payload(self, obj, *, title=None):
        return {
            "author": str(obj.author_id or ""),
            "review_note": obj.review_note,
            "published_at_0": "",
            "published_at_1": "",
            "tools": [],
            "slug": obj.safe_translation_getter("slug", language_code="en"),
            "title": title if title is not None else obj.safe_translation_getter(
                "title", language_code="en"
            ),
            "intro": "intro",
            "body": "body",
            "outro": "outro",
            "_continue": "Save",
        }

    def test_a_submit_revision_stores_no_provable_review_binding(self):
        obj = self.make()
        review = self.drive_to(self.prefix, Prompt, obj.pk, Workflow.STATUS_REVIEW)
        version = self.latest_version_for(Prompt, obj.pk)
        target = self.target_fields(version)

        self.assertEqual(target["status"], Workflow.STATUS_REVIEW)
        self.assertIsNone(target["review_revision"])
        # ... while the live row *was* bound right after the snapshot.
        self.assertIsNotNone(review.review_revision_id)

    def test_rollback_to_a_submit_revision_restores_the_review_binding(self):
        obj = self.make()
        review = self.drive_to(self.prefix, Prompt, obj.pk, Workflow.STATUS_REVIEW)
        version = self.latest_version_for(Prompt, obj.pk)
        selected_revision_id = version.revision_id

        self.client.post(change_url(self.prefix, obj.pk), self.payload(review, title="Content B"))
        self.rollback(self.prefix, obj.pk, version, self.payload(review))

        after = refetch(Prompt, obj.pk)
        self.assertEqual(after.status, Workflow.STATUS_REVIEW)
        self.assertEqual(after.review_revision_id, selected_revision_id)
        self.assertIsNone(after.approved_revision_id)
        self.assertTrue(validate_review_binding(after).is_valid)
        # The content came back exactly as the revision stored it.
        translated = self.target_translation_fields(version, PromptTranslation)
        self.assertEqual(
            after.safe_translation_getter("title", language_code="en"),
            translated["title"],
        )


class GuideExactReversionTests(ExactReversionTestCase):
    prefix = "guides_guide"
    model = Guide

    def make(self, *, title="Guide A"):
        with translation.override("en"):
            obj = Guide.objects.create(author=self.editor, status=Workflow.STATUS_DRAFT)
            obj.create_translation(
                "en", title=title, intro="intro", body="body", slug=_unique("d3c-guide")
            )
        return obj

    def payload(self, obj, *, title=None, sections=()):
        data = {
            "author": str(obj.author_id or ""),
            "review_note": obj.review_note,
            "published_at_0": "",
            "published_at_1": "",
            "categories": [],
            "tools": [],
            "slug": obj.safe_translation_getter("slug", language_code="en"),
            "title": title if title is not None else obj.safe_translation_getter(
                "title", language_code="en"
            ),
            "intro": "intro",
            "body": "body",
            "sections-TOTAL_FORMS": str(len(sections)),
            "sections-INITIAL_FORMS": str(len([s for s in sections if s.get("id")])),
            "sections-MIN_NUM_FORMS": "0",
            "sections-MAX_NUM_FORMS": "1000",
            "_continue": "Save",
        }
        for index, section in enumerate(sections):
            for key, value in section.items():
                data[f"sections-{index}-{key}"] = value
        return data

    def _section_form(self, section):
        return {
            "id": str(section.pk),
            "guide": str(section.guide_id),
            "order": str(section.order),
            "title": section.safe_translation_getter("title", language_code="en") or "",
            "body": section.safe_translation_getter("body", language_code="en") or "",
        }

    def test_rollback_to_published_stays_published(self):
        obj = self.make()
        published = self.drive_to(self.prefix, Guide, obj.pk, Workflow.STATUS_PUBLISHED)
        version = self.latest_version_for(Guide, obj.pk)
        target = self.target_fields(version)
        self.assertTrue(target["is_published"])
        self.assertTrue(target["live_i18n"])

        self.client.post(change_url(self.prefix, obj.pk), self.payload(published, title="Guide B"))
        self.rollback(self.prefix, obj.pk, version, self.payload(published))
        self.assert_matches_target_revision(
            Guide, obj.pk, version, translation_model=GuideTranslation
        )

    def test_rollback_to_review_stays_review(self):
        obj = self.make()
        review = self.drive_to(self.prefix, Guide, obj.pk, Workflow.STATUS_REVIEW)
        version = self.latest_version_for(Guide, obj.pk)
        self.assertEqual(self.target_fields(version)["status"], Workflow.STATUS_REVIEW)

        self.client.post(change_url(self.prefix, obj.pk), self.payload(review, title="Guide B"))
        self.rollback(self.prefix, obj.pk, version, self.payload(review))
        self.assert_matches_target_revision(
            Guide, obj.pk, version, translation_model=GuideTranslation
        )

    def test_rollback_to_archived_stays_archived(self):
        obj = self.make()
        self.drive_to(self.prefix, Guide, obj.pk, Workflow.STATUS_PUBLISHED)
        archived = self.drive_to(self.prefix, Guide, obj.pk, Workflow.STATUS_ARCHIVED)
        version = self.latest_version_for(Guide, obj.pk)
        self.assertEqual(self.target_fields(version)["status"], Workflow.STATUS_ARCHIVED)

        self.run_action(self.prefix, "action_restore_draft", obj.pk)
        self.rollback(self.prefix, obj.pk, version, self.payload(archived))
        self.assert_matches_target_revision(
            Guide, obj.pk, version, translation_model=GuideTranslation
        )

    def test_later_section_and_item_are_removed_by_the_rollback(self):
        obj = self.make()
        published = self.drive_to(self.prefix, Guide, obj.pk, Workflow.STATUS_PUBLISHED)
        version = self.latest_version_for(Guide, obj.pk)

        section = GuideSection.objects.create(guide=obj, order=0)
        section.create_translation("en", title="Later section", body="b")
        item = GuideItem.objects.create(section=section, order=0, url="https://example.com/a")
        item.create_translation("en", title="Later item", teaser="t")

        self.rollback(self.prefix, obj.pk, version, self.payload(published))

        self.assertFalse(GuideSection.objects.filter(pk=section.pk).exists())
        self.assertFalse(GuideItem.objects.filter(pk=item.pk).exists())
        self.assertTrue(Guide.objects.filter(pk=obj.pk).exists())
        self.assertEqual(refetch(Guide, obj.pk).status, Workflow.STATUS_PUBLISHED)

    def test_sections_present_in_the_target_revision_survive(self):
        obj = self.make()
        section = GuideSection.objects.create(guide=obj, order=0)
        section.create_translation("en", title="Kept section", body="b")
        published = self.drive_to(self.prefix, Guide, obj.pk, Workflow.STATUS_PUBLISHED)
        version = self.latest_version_for(Guide, obj.pk)

        self.client.post(
            change_url(self.prefix, obj.pk),
            self.payload(published, title="Guide B", sections=[self._section_form(section)]),
        )
        self.rollback(
            self.prefix, obj.pk, version,
            self.payload(published, sections=[self._section_form(section)]),
        )

        self.assertTrue(GuideSection.objects.filter(pk=section.pk).exists())
        self.assertEqual(refetch(Guide, obj.pk).status, Workflow.STATUS_PUBLISHED)

    def test_a_normal_edit_after_the_rollback_still_invalidates(self):
        obj = self.make()
        published = self.drive_to(self.prefix, Guide, obj.pk, Workflow.STATUS_PUBLISHED)
        version = self.latest_version_for(Guide, obj.pk)

        self.client.post(change_url(self.prefix, obj.pk), self.payload(published, title="Guide B"))
        self.rollback(self.prefix, obj.pk, version, self.payload(published))
        self.assertEqual(refetch(Guide, obj.pk).status, Workflow.STATUS_PUBLISHED)

        restored = refetch(Guide, obj.pk)
        self.client.post(change_url(self.prefix, obj.pk), self.payload(restored, title="Guide C"))
        after = refetch(Guide, obj.pk)
        self.assertNotEqual(after.status, Workflow.STATUS_PUBLISHED)
        self.assertEqual(
            after.safe_translation_getter("title", language_code="en"), "Guide C"
        )


class UseCaseExactReversionTests(ExactReversionTestCase):
    prefix = "usecases_usecase"
    model = UseCase

    def make(self, *, title="Case A"):
        with translation.override("en"):
            obj = UseCase.objects.create(author=self.editor, status=Workflow.STATUS_DRAFT)
            obj.create_translation(
                "en", title=title, intro="intro", body="body", outro="outro",
                persona="persona", slug=_unique("d3c-case"),
            )
        return obj

    def payload(self, obj, *, title=None):
        return {
            "author": str(obj.author_id or ""),
            "review_note": obj.review_note,
            "published_at_0": "",
            "published_at_1": "",
            "tools": [],
            "slug": obj.safe_translation_getter("slug", language_code="en"),
            "title": title if title is not None else obj.safe_translation_getter(
                "title", language_code="en"
            ),
            "intro": "intro",
            "body": "body",
            "outro": "outro",
            "persona": "persona",
            "_continue": "Save",
        }

    def test_rollback_to_published_stays_published(self):
        obj = self.make()
        published = self.drive_to(self.prefix, UseCase, obj.pk, Workflow.STATUS_PUBLISHED)
        version = self.latest_version_for(UseCase, obj.pk)
        self.assertTrue(self.target_fields(version)["is_published"])

        self.client.post(change_url(self.prefix, obj.pk), self.payload(published, title="Case B"))
        self.rollback(self.prefix, obj.pk, version, self.payload(published))
        self.assert_matches_target_revision(
            UseCase, obj.pk, version, translation_model=UseCaseTranslation
        )

    def test_rollback_to_approved_stays_approved(self):
        obj = self.make()
        approved = self.drive_to(self.prefix, UseCase, obj.pk, Workflow.STATUS_APPROVED)
        version = self.latest_version_for(UseCase, obj.pk)
        self.assertEqual(self.target_fields(version)["status"], Workflow.STATUS_APPROVED)

        self.client.post(change_url(self.prefix, obj.pk), self.payload(approved, title="Case B"))
        self.rollback(self.prefix, obj.pk, version, self.payload(approved))
        self.assert_matches_target_revision(
            UseCase, obj.pk, version, translation_model=UseCaseTranslation
        )

    def test_later_translation_is_removed_by_the_rollback(self):
        obj = self.make()
        published = self.drive_to(self.prefix, UseCase, obj.pk, Workflow.STATUS_PUBLISHED)
        version = self.latest_version_for(UseCase, obj.pk)

        refetch(UseCase, obj.pk).create_translation(
            "de", title="DE", intro="i", body="b", outro="o", persona="p",
            slug=_unique("d3c-case-de"),
        )
        self.rollback(self.prefix, obj.pk, version, self.payload(published))

        after = refetch(UseCase, obj.pk)
        self.assertEqual(list(after.get_available_languages()), ["en"])
        self.assertEqual(after.status, Workflow.STATUS_PUBLISHED)


class ComparisonExactReversionTests(ExactReversionTestCase):
    prefix = "compare_comparison"
    model = Comparison

    def make(self, *, title="CMP A"):
        with translation.override("en"):
            self.tool = Tool.objects.create(slug=_unique("d3c-tool"))
            self.tool.create_translation("en", name="Tool")
            obj = Comparison.objects.create(
                author=self.editor, status=Workflow.STATUS_DRAFT
            )
            obj.create_translation(
                "en", title=title, intro="intro", body="body", slug=_unique("d3c-cmp")
            )
        return obj

    def payload(self, obj, *, title=None, entries=()):
        data = {
            "author": str(obj.author_id or ""),
            "review_note": obj.review_note,
            "slug": obj.safe_translation_getter("slug", language_code="en"),
            "title": title if title is not None else obj.safe_translation_getter(
                "title", language_code="en"
            ),
            "intro": "intro",
            "body": "body",
            "tool_entries-TOTAL_FORMS": str(len(entries)),
            "tool_entries-INITIAL_FORMS": str(len([e for e in entries if e.get("id")])),
            "tool_entries-MIN_NUM_FORMS": "0",
            "tool_entries-MAX_NUM_FORMS": "1000",
            "_continue": "Save",
        }
        for index, entry in enumerate(entries):
            for key, value in entry.items():
                data[f"tool_entries-{index}-{key}"] = value
        return data

    def _entry_form(self, entry):
        return {
            "id": str(entry.pk),
            "comparison": str(entry.comparison_id),
            "tool": str(entry.tool_id),
            "position": str(entry.position),
            "label": entry.safe_translation_getter("label", language_code="en") or "",
            "summary": entry.safe_translation_getter("summary", language_code="en") or "",
            "pros": "",
            "cons": "",
            "special": "",
        }

    def test_rollback_to_published_restores_live_entries(self):
        obj = self.make()
        entry = ComparisonToolEntry.objects.create(
            comparison=obj, tool=self.tool, position=0
        )
        entry.create_translation("en", label="Entry A", summary="s")
        published = self.drive_to(
            self.prefix, Comparison, obj.pk, Workflow.STATUS_PUBLISHED
        )
        version = self.latest_version_for(Comparison, obj.pk)
        target = self.target_fields(version)
        self.assertTrue(target["is_published"])
        self.assertIsNotNone(target["live_entries"])

        self.client.post(
            change_url(self.prefix, obj.pk),
            self.payload(published, title="CMP B", entries=[self._entry_form(entry)]),
        )
        self.rollback(
            self.prefix, obj.pk, version,
            self.payload(published, entries=[self._entry_form(entry)]),
        )
        self.assert_matches_target_revision(
            Comparison, obj.pk, version,
            translation_model=ComparisonTranslation,
            extra_fields=(("live_entries", "live_entries"),),
        )
        self.assertTrue(ComparisonToolEntry.objects.filter(pk=entry.pk).exists())

    def test_later_entry_is_removed_by_the_rollback(self):
        obj = self.make()
        published = self.drive_to(
            self.prefix, Comparison, obj.pk, Workflow.STATUS_PUBLISHED
        )
        version = self.latest_version_for(Comparison, obj.pk)

        later = ComparisonToolEntry.objects.create(
            comparison=obj, tool=self.tool, position=1
        )
        later.create_translation("en", label="Later", summary="s")

        self.rollback(self.prefix, obj.pk, version, self.payload(published))

        self.assertFalse(ComparisonToolEntry.objects.filter(pk=later.pk).exists())
        self.assertTrue(Comparison.objects.filter(pk=obj.pk).exists())
        self.assertEqual(refetch(Comparison, obj.pk).status, Workflow.STATUS_PUBLISHED)


# ======================================================================
# Beta 11.11D3C: the exact-reversion marker belongs to reverts only
# ======================================================================


MARKER_ATTR = "_mentoro_editorial_reversion_form"


class ReversionMarkerScopeTests(ExactReversionTestCase):
    """
    ``recover_view`` is not a revert of a working state - it restores a
    previously hard-deleted object and keeps its pre-D3C contract. The
    exact-reversion marker (which suppresses the shared published -> review
    auto-transition) must therefore be set for ``revision_view`` only.

    Probed directly on the ``HttpRequest`` inside ``save_model``, which is the
    single place the marker is consulted from - no guessing about a status
    that reversion, parler and the auto-review together happen to produce.
    """

    def _spy_on_marker(self, admin_class):
        """Records the marker's value on the request for each ``save_model``."""
        seen = []
        original = admin_class.save_model

        def spy(admin_self, request, obj, form, change):
            seen.append(getattr(request, MARKER_ATTR, False))
            return original(admin_self, request, obj, form, change)

        return seen, mock.patch.object(admin_class, "save_model", spy)

    def _guide(self):
        with translation.override("en"):
            obj = Guide.objects.create(author=self.editor, status=Workflow.STATUS_DRAFT)
            obj.create_translation(
                "en", title="Guide A", intro="intro", body="body",
                slug=_unique("d3c-marker-guide"),
            )
        return obj

    def _guide_payload(self, *, slug, title):
        return {
            "author": str(self.editor.pk),
            "review_note": "",
            "published_at_0": "",
            "published_at_1": "",
            "categories": [],
            "tools": [],
            "slug": slug,
            "title": title,
            "intro": "intro",
            "body": "body",
            "sections-TOTAL_FORMS": "0",
            "sections-INITIAL_FORMS": "0",
            "sections-MIN_NUM_FORMS": "0",
            "sections-MAX_NUM_FORMS": "1000",
            "_continue": "Save",
        }

    def test_the_marker_is_set_during_a_revert(self):
        from guides.admin import GuideAdmin

        obj = self._guide()
        published = self.drive_to("guides_guide", Guide, obj.pk, Workflow.STATUS_PUBLISHED)
        version = self.latest_version_for(Guide, obj.pk)
        slug = published.safe_translation_getter("slug", language_code="en")

        seen, patcher = self._spy_on_marker(GuideAdmin)
        with patcher:
            resp = self.client.post(
                revision_url("guides_guide", obj.pk, version.pk),
                data=self._guide_payload(slug=slug, title="Guide A"),
            )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(seen, [True])

    def test_the_marker_is_not_set_during_a_recover(self):
        from guides.admin import GuideAdmin

        obj = self._guide()
        published = self.drive_to("guides_guide", Guide, obj.pk, Workflow.STATUS_PUBLISHED)
        version = self.latest_version_for(Guide, obj.pk)
        slug = published.safe_translation_getter("slug", language_code="en")
        Guide.objects.filter(pk=obj.pk).delete()

        seen, patcher = self._spy_on_marker(GuideAdmin)
        with patcher:
            resp = self.client.post(
                reverse("admin:guides_guide_recover", args=[version.pk]),
                data=self._guide_payload(slug=slug, title="Guide A"),
            )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(seen, [False])
        self.assertTrue(Guide.objects.filter(pk=obj.pk).exists())

    def test_the_marker_is_not_set_during_an_ordinary_changeform(self):
        from guides.admin import GuideAdmin

        obj = self._guide()
        published = self.drive_to("guides_guide", Guide, obj.pk, Workflow.STATUS_PUBLISHED)
        slug = published.safe_translation_getter("slug", language_code="en")

        seen, patcher = self._spy_on_marker(GuideAdmin)
        with patcher:
            resp = self.client.post(
                change_url("guides_guide", obj.pk),
                data=self._guide_payload(slug=slug, title="Guide B"),
            )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(seen, [False])

    def test_the_marker_does_not_leak_into_the_next_request(self):
        from guides.admin import GuideAdmin

        obj = self._guide()
        published = self.drive_to("guides_guide", Guide, obj.pk, Workflow.STATUS_PUBLISHED)
        version = self.latest_version_for(Guide, obj.pk)
        slug = published.safe_translation_getter("slug", language_code="en")

        self.client.post(
            revision_url("guides_guide", obj.pk, version.pk),
            data=self._guide_payload(slug=slug, title="Guide A"),
        )

        seen, patcher = self._spy_on_marker(GuideAdmin)
        with patcher:
            self.client.post(
                change_url("guides_guide", obj.pk),
                data=self._guide_payload(slug=slug, title="Guide C"),
            )
        self.assertEqual(seen, [False])
        # ... and the ordinary invalidation contract is back in force.
        self.assertNotEqual(refetch(Guide, obj.pk).status, Workflow.STATUS_PUBLISHED)

    def test_a_prompt_recover_stays_on_the_recovery_contract(self):
        from prompts.admin import PromptAdmin

        with translation.override("en"):
            prompt = Prompt.objects.create(
                author=self.editor, status=Workflow.STATUS_DRAFT
            )
            slug = _unique("d3c-marker-prompt")
            prompt.create_translation(
                "en", title="P", intro="i", body="b", outro="o", slug=slug
            )
        self.drive_to("prompts_prompt", Prompt, prompt.pk, Workflow.STATUS_PUBLISHED)
        version = self.latest_version_for(Prompt, prompt.pk)
        Prompt.objects.filter(pk=prompt.pk).delete()

        seen, patcher = self._spy_on_marker(PromptAdmin)
        with patcher:
            resp = self.client.post(
                reverse("admin:prompts_prompt_recover", args=[version.pk]),
                data={
                    "author": str(self.editor.pk),
                    "review_note": "",
                    "published_at_0": "",
                    "published_at_1": "",
                    "tools": [],
                    "slug": slug,
                    "title": "P",
                    "intro": "i",
                    "body": "b",
                    "outro": "o",
                    "_continue": "Save",
                },
            )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(seen, [False])
        self.assertTrue(Prompt.objects.filter(pk=prompt.pk).exists())
