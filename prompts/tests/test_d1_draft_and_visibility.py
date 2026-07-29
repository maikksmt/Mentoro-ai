"""
Beta 11.11D1: automatic review invalidation always targets ``draft``, and a
previously published prompt stays publicly visible through its last live
snapshot while a new draft is being worked on.

Two contracts, deliberately tested together because they only make sense
together: before D1, the automatic invalidation of a ``review``/``approved``
row with a live snapshot targeted ``rework`` *precisely so that the row would
stay inside ``LIVE_EDITING_STATUSES`` and keep serving its published page*.
D1 separates those two concerns:

* the workflow status now records only what an editor actually decided -
  ``rework`` is reachable exclusively through an explicit
  ``request_rework``, never through an automatic payload invalidation;
* public visibility is decided by proof of a previous publication
  (``is_published`` plus a usable ``live_i18n`` snapshot, and never
  ``archived``), not by which editing status the row happens to sit in.

Everything public still comes exclusively from the snapshot: the new draft
title, body and slug must never appear on the list, the detail page, in
search or in the sitemap, and the language contract stays strictly
no-fallback.
"""
import itertools

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import translation
from django.utils.translation import override as override_language

from core.models.editorial import EditorialWorkflowMixin as Workflow
from core.review_binding import (
    invalidate_editorial_review_state,
    target_status_after_review_invalidation,
)
from prompts.models import Prompt, PromptTranslation
from prompts.review_approval import approve_prompt_review
from prompts.review_submission import submit_prompt_for_review

User = get_user_model()

_counter = itertools.count()

CHANGELIST_URL = reverse("admin:prompts_prompt_changelist")

LIVE_TITLE = "Veroeffentlichter Titel"
LIVE_BODY = "Veroeffentlichter Inhalt"
DRAFT_TITLE = "Neuer ungeprueft Titel"
DRAFT_BODY = "Neuer ungeprueft Inhalt"


def refetch(prompt):
    """``status`` is a protected FSMField, so ``refresh_from_db()``'s plain
    setattr is rejected - reload through the manager instead."""
    return Prompt.objects.get(pk=prompt.pk)


def change_url(prompt):
    return reverse("admin:prompts_prompt_change", args=[prompt.pk])


class D1TestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.editor = User.objects.create_user("d1-editor", password="pw", is_staff=True)
        cls.editor.groups.add(Group.objects.get_or_create(name="Editor")[0])
        cls.author = User.objects.create_user("d1-author", password="pw", is_staff=True)
        cls.author.groups.add(Group.objects.get_or_create(name="Author")[0])

    def setUp(self):
        self.client.force_login(self.editor)
        # Several tests here fetch a `/de/` URL to prove the no-fallback
        # language contract. Django's LocaleMiddleware activates that language
        # for the request and never deactivates it afterwards, so without this
        # cleanup German stays active for the rest of the process and any
        # later test that reverse()s an i18n URL or asserts on a translated
        # string silently gets the German one. Same pattern as
        # core/tests/test_admin_richtext_security.py and
        # catalog/tests/test_filters_and_pagination.py.
        self.addCleanup(translation.activate, settings.LANGUAGE_CODE)

    # -- construction helpers ------------------------------------------

    def make_prompt(self, *, status=Workflow.STATUS_DRAFT, author=None,
                    title=LIVE_TITLE, body=LIVE_BODY, slug=None, language="en", **extra):
        prompt = Prompt.objects.create(status=status, author=author or self.author, **extra)
        prompt.create_translation(
            language, title=title, intro="intro", body=body, outro="outro",
            slug=slug or f"d1-slug-{next(_counter)}",
        )
        return prompt

    def submit(self, prompt):
        submit_prompt_for_review(refetch(prompt), actor=self.editor)
        return refetch(prompt)

    def approve(self, prompt):
        approve_prompt_review(refetch(prompt), actor=self.editor)
        return refetch(prompt)

    def run_action(self, action, prompt):
        resp = self.client.post(
            CHANGELIST_URL,
            data={"action": action, "_selected_action": [str(prompt.pk)], "index": "0"},
            follow=True,
        )
        assert resp.status_code == 200, resp.content
        return refetch(prompt)

    def publish_via_admin(self, prompt):
        return self.run_action("action_publish", prompt)

    def really_published(self, *, title=LIVE_TITLE, body=LIVE_BODY, slug=None, author=None):
        """
        A genuinely published prompt - never a hand-set ``status="published"``.
        Runs the real Beta 11.11C2A submit, the real C3A approval and the real
        admin publish action, so ``live_i18n``, ``public_slug``,
        ``live_author``, ``is_published`` and ``published_at`` are all written
        by production code.
        """
        prompt = self.make_prompt(title=title, body=body, slug=slug, author=author)
        self.submit(prompt)
        self.approve(prompt)
        published = self.publish_via_admin(prompt)
        assert published.status == Workflow.STATUS_PUBLISHED, published.status
        assert published.is_published is True
        assert published.live_i18n, "publish must write a live snapshot"
        return published

    def admin_edit(self, prompt, *, actor=None, **overrides):
        """A real PromptAdmin changeform POST."""
        actor = actor or self.author
        fresh = refetch(prompt)
        data = {
            "author": str(fresh.author_id) if fresh.author_id else "",
            "review_note": fresh.review_note,
            "published_at_0": "", "published_at_1": "",
            "tools": [],
            "slug": fresh.safe_translation_getter("slug", language_code="en"),
            "title": fresh.safe_translation_getter("title", language_code="en"),
            "intro": "intro",
            "body": fresh.safe_translation_getter("body", language_code="en"),
            "outro": "outro",
            "_continue": "Save",
        }
        data.update(overrides)
        self.client.force_login(actor)
        resp = self.client.post(change_url(prompt), data)
        self.client.force_login(self.editor)
        assert resp.status_code == 302, resp.content
        return refetch(prompt)

    @property
    def public(self):
        """
        A fresh, anonymous client for every public assertion.

        ``self.client`` stays logged in as staff and carries the Django admin's
        own "was changed successfully" message in its session after an admin
        POST; that message renders the *draft* title into the very next page
        the same client requests, which would masquerade as a public draft
        leak. The public site is anonymous, so test it anonymously.
        """
        return Client()

    def live_slug(self, prompt, language="en"):
        return (refetch(prompt).live_i18n or {}).get(language, {}).get("slug")

    def published_then_resubmitted(self):
        """
        A prompt that is genuinely in ``review`` *and* carries a live
        snapshot - reached the only way production allows it: publish, edit
        (which drops it back into the editing statuses while keeping
        ``live_i18n``/``is_published``), then submit through the real Beta
        11.11C2A primitive. ``published`` itself is deliberately not
        submittable (``_SUBMITTABLE_STATUSES``), so a direct
        published -> review shortcut does not exist.
        """
        prompt = self.really_published()
        edited = self.admin_edit(prompt, title=f"resubmit-{next(_counter)}")
        self.assertIn(
            edited.status, (Workflow.STATUS_DRAFT, Workflow.STATUS_REWORK)
        )
        self.assertTrue(edited.live_i18n)
        self.assertTrue(edited.is_published)
        return self.submit(prompt)


# ======================================================================
# 5.1 Automatic invalidation always targets draft
# ======================================================================


class AutomaticInvalidationTargetsDraftTests(D1TestCase):
    """The central D1 rule, at the one place that decides it."""

    def _mutate_payload(self, prompt):
        PromptTranslation.objects.filter(master_id=prompt.pk).update(
            title=f"changed-{next(_counter)}"
        )

    def test_pure_target_function_is_always_draft(self):
        without = self.make_prompt()
        with_live = self.make_prompt()
        Prompt.objects.filter(pk=with_live.pk).update(live_i18n={"en": {"title": "Live"}})

        self.assertEqual(
            target_status_after_review_invalidation(refetch(without)), Workflow.STATUS_DRAFT
        )
        self.assertEqual(
            target_status_after_review_invalidation(refetch(with_live)), Workflow.STATUS_DRAFT
        )

    def test_review_without_live_snapshot_invalidates_to_draft(self):
        prompt = self.submit(self.make_prompt())
        self.assertEqual(refetch(prompt).live_i18n, {})
        self._mutate_payload(prompt)
        result = invalidate_editorial_review_state(refetch(prompt))
        self.assertTrue(result.changed)
        self.assertEqual(result.current_status, Workflow.STATUS_DRAFT)
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_DRAFT)

    def test_review_with_live_snapshot_invalidates_to_draft(self):
        prompt = self.published_then_resubmitted()
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_REVIEW)
        self._mutate_payload(prompt)
        before = refetch(prompt)

        result = invalidate_editorial_review_state(refetch(prompt))

        self.assertTrue(result.changed)
        self.assertEqual(result.current_status, Workflow.STATUS_DRAFT)
        after = refetch(prompt)
        self.assertEqual(after.status, Workflow.STATUS_DRAFT)
        # bindings cleared
        self.assertIsNone(after.review_revision_id)
        self.assertIsNone(after.approved_revision_id)
        self.assertEqual(after.review_payload_fingerprint, "")
        self.assertIsNone(after.reviewed_by_id)
        self.assertIsNone(after.reviewed_at)
        self.assertIsNone(after.submitted_for_review_at)
        # publication proof + snapshot preserved
        self.assertEqual(after.live_i18n, before.live_i18n)
        self.assertEqual(after.live_author, before.live_author)
        self.assertTrue(after.is_published)
        self.assertEqual(after.published_at, before.published_at)
        self.assertEqual(
            after.last_published_revision_id, before.last_published_revision_id
        )
        self.assertEqual(after.review_note, before.review_note)
        self.assertEqual(
            after.translations.get(language_code="en").public_slug,
            before.translations.get(language_code="en").public_slug,
        )

    def test_approved_without_live_snapshot_invalidates_to_draft(self):
        prompt = self.approve(self.submit(self.make_prompt()))
        self.assertEqual(refetch(prompt).live_i18n, {})
        self._mutate_payload(prompt)
        result = invalidate_editorial_review_state(refetch(prompt))
        self.assertEqual(result.current_status, Workflow.STATUS_DRAFT)

    def test_approved_with_live_snapshot_invalidates_to_draft(self):
        prompt = self.published_then_resubmitted()
        self.approve(prompt)
        self.assertEqual(refetch(prompt).status, Workflow.STATUS_APPROVED)
        self._mutate_payload(prompt)

        result = invalidate_editorial_review_state(refetch(prompt))

        self.assertEqual(result.current_status, Workflow.STATUS_DRAFT)
        after = refetch(prompt)
        self.assertEqual(after.status, Workflow.STATUS_DRAFT)
        self.assertTrue(after.is_published)
        self.assertTrue(after.live_i18n)

    def test_no_runtime_path_produces_rework_automatically(self):
        """The private rework invalidation transition must no longer exist as
        a runtime path at all."""
        self.assertFalse(
            hasattr(Prompt, "_invalidate_review_to_rework"),
            "_invalidate_review_to_rework must be gone from runtime code",
        )


# ======================================================================
# 5.2 Explicit request_rework is the only runtime producer of rework
# ======================================================================


class ExplicitRequestReworkTests(D1TestCase):
    def test_editor_request_rework_produces_rework(self):
        prompt = self.submit(self.make_prompt())
        reloaded = self.run_action("action_request_rework", prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_REWORK)

    def test_request_rework_keeps_an_existing_live_snapshot(self):
        prompt = self.published_then_resubmitted()
        before_live = refetch(prompt).live_i18n
        self.assertTrue(before_live)
        reloaded = self.run_action("action_request_rework", prompt)
        self.assertEqual(reloaded.status, Workflow.STATUS_REWORK)
        self.assertEqual(reloaded.live_i18n, before_live)
        self.assertTrue(reloaded.is_published)

    def test_author_cannot_request_rework_on_their_own_prompt(self):
        prompt = self.submit(self.make_prompt(author=self.author))
        self.client.force_login(self.author)
        reloaded = self.run_action("action_request_rework", prompt)
        self.client.force_login(self.editor)
        self.assertEqual(reloaded.status, Workflow.STATUS_REVIEW)


# ======================================================================
# 5.3 A real edit of a really published prompt ends in draft
# ======================================================================


class PublishedEditEndsInDraftTests(D1TestCase):
    def test_author_edit_of_published_prompt_ends_in_draft(self):
        prompt = self.really_published()
        before = refetch(prompt)

        reloaded = self.admin_edit(prompt, title=DRAFT_TITLE, body=DRAFT_BODY)

        self.assertEqual(reloaded.status, Workflow.STATUS_DRAFT)
        self.assertIsNone(reloaded.review_revision_id)
        self.assertIsNone(reloaded.approved_revision_id)
        self.assertEqual(reloaded.review_payload_fingerprint, "")
        # the published projection is untouched
        self.assertEqual(reloaded.live_i18n, before.live_i18n)
        self.assertTrue(reloaded.is_published)
        # the new draft really was saved
        self.assertEqual(reloaded.translations.get(language_code="en").title, DRAFT_TITLE)

    def test_final_revision_records_draft_not_review_or_rework(self):
        import json

        from reversion.models import Revision

        prompt = self.really_published()
        self.admin_edit(prompt, title=DRAFT_TITLE)

        revision = Revision.objects.latest("pk")
        root = revision.version_set.get(
            content_type__app_label="prompts",
            content_type__model="prompt",
            object_id=str(prompt.pk),
        )
        fields = json.loads(root.serialized_data)[0]["fields"]
        self.assertEqual(fields["status"], Workflow.STATUS_DRAFT)
        self.assertIsNone(fields["review_revision"])


# ======================================================================
# 5.4 / 5.7 Public visibility of the old live version during a new draft
# ======================================================================


class LiveSnapshotStaysPublicTests(D1TestCase):
    def _published_then_edited(self):
        prompt = self.really_published(title=LIVE_TITLE, body=LIVE_BODY)
        live_slug = self.live_slug(prompt)
        draft_slug = f"neuer-draft-slug-{next(_counter)}"
        edited = self.admin_edit(
            prompt, title=DRAFT_TITLE, body=DRAFT_BODY, slug=draft_slug
        )
        self.assertEqual(edited.status, Workflow.STATUS_DRAFT)
        return edited, live_slug, draft_slug

    def test_list_still_contains_the_prompt_with_published_title_only(self):
        prompt, _live_slug, _draft = self._published_then_edited()
        resp = self.public.get(reverse("prompts:list"))
        html = resp.content.decode()
        self.assertEqual(resp.status_code, 200)
        self.assertIn(LIVE_TITLE, html)
        self.assertNotIn(DRAFT_TITLE, html)

    def test_detail_resolves_through_the_old_live_slug_only(self):
        prompt, live_slug, draft_slug = self._published_then_edited()

        ok = self.public.get(reverse("prompts:detail", kwargs={"slug": live_slug}))
        self.assertEqual(ok.status_code, 200)
        html = ok.content.decode()
        self.assertIn(LIVE_TITLE, html)
        self.assertIn(LIVE_BODY, html)
        self.assertNotIn(DRAFT_TITLE, html)
        self.assertNotIn(DRAFT_BODY, html)

        gone = self.public.get(reverse("prompts:detail", kwargs={"slug": draft_slug}))
        self.assertEqual(gone.status_code, 404)

    def test_search_uses_live_data_only(self):
        from search.adapters.prompts import PromptSearchAdapter
        from search.query import normalize_search_query

        prompt, _live_slug, _draft = self._published_then_edited()
        adapter = PromptSearchAdapter()

        hits = adapter.search(
            query=normalize_search_query("Veroeffentlichter"), language_code="en"
        )
        self.assertTrue(any(r.object_id == prompt.pk for r in hits))

        draft_hits = adapter.search(
            query=normalize_search_query("ungeprueft"), language_code="en"
        )
        self.assertFalse(any(r.object_id == prompt.pk for r in draft_hits))

    def test_sitemap_contains_the_live_url_only(self):
        from core.sitemaps import PromptSitemap

        prompt, live_slug, draft_slug = self._published_then_edited()
        with override_language("en"):
            locations = [PromptSitemap().location(o) for o in PromptSitemap().items()]
        joined = " ".join(locations)
        self.assertIn(live_slug, joined)
        self.assertNotIn(draft_slug, joined)

    def test_visibility_does_not_depend_on_the_legacy_marker_alone(self):
        """
        Beta 11.11C4J-R3 found that the editorial-view publish path never
        sets ``last_published_revision_id``. D1's proof of a previous
        publication must therefore not be that marker alone: clearing it on a
        genuinely published prompt must not take the live version offline.
        """
        prompt, live_slug, _draft = self._published_then_edited()
        Prompt.objects.filter(pk=prompt.pk).update(last_published_revision_id=None)

        resp = self.public.get(reverse("prompts:detail", kwargs={"slug": live_slug}))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(LIVE_TITLE, resp.content.decode())


# ======================================================================
# 5.5 Never-published content stays invisible in every editing status
# ======================================================================


class NeverPublishedStaysInvisibleTests(D1TestCase):
    def _assert_invisible(self, prompt, slug):
        self.assertFalse(refetch(prompt).is_published)
        self.assertEqual(refetch(prompt).live_i18n, {})

        listing = self.public.get(reverse("prompts:list"))
        self.assertNotIn(LIVE_TITLE, listing.content.decode())

        detail = self.public.get(reverse("prompts:detail", kwargs={"slug": slug}))
        self.assertEqual(detail.status_code, 404)

        self.assertNotIn(
            prompt.pk, list(Prompt.objects.visible_in_language("en").values_list("pk", flat=True))
        )

    def test_never_published_draft_is_invisible(self):
        slug = f"d1-never-draft-{next(_counter)}"
        prompt = self.make_prompt(slug=slug)
        self._assert_invisible(prompt, slug)

    def test_never_published_review_is_invisible(self):
        slug = f"d1-never-review-{next(_counter)}"
        prompt = self.submit(self.make_prompt(slug=slug))
        self._assert_invisible(prompt, slug)

    def test_never_published_approved_is_invisible(self):
        slug = f"d1-never-approved-{next(_counter)}"
        prompt = self.approve(self.submit(self.make_prompt(slug=slug)))
        self._assert_invisible(prompt, slug)

    def test_never_published_rework_is_invisible(self):
        slug = f"d1-never-rework-{next(_counter)}"
        prompt = self.submit(self.make_prompt(slug=slug))
        prompt = self.run_action("action_request_rework", prompt)
        self.assertEqual(prompt.status, Workflow.STATUS_REWORK)
        self._assert_invisible(prompt, slug)

    def test_archived_after_publication_is_invisible(self):
        prompt = self.really_published()
        live_slug = self.live_slug(prompt)
        archived = self.run_action("action_archive", prompt)
        self.assertEqual(archived.status, Workflow.STATUS_ARCHIVED)

        detail = self.public.get(reverse("prompts:detail", kwargs={"slug": live_slug}))
        self.assertEqual(detail.status_code, 404)
        self.assertNotIn(
            prompt.pk, list(Prompt.objects.visible_in_language("en").values_list("pk", flat=True))
        )

    def test_restored_from_archive_stays_invisible(self):
        """Archiving is the deliberate public withdrawal; restoring it into
        the workflow must not silently republish the old snapshot."""
        prompt = self.really_published()
        live_slug = self.live_slug(prompt)
        self.run_action("action_archive", prompt)
        restored = self.run_action("action_restore_draft", prompt)
        self.assertEqual(restored.status, Workflow.STATUS_DRAFT)

        detail = self.public.get(reverse("prompts:detail", kwargs={"slug": live_slug}))
        self.assertEqual(detail.status_code, 404)


# ======================================================================
# 5.6 Language contract stays strictly no-fallback
# ======================================================================


class LanguageContractTests(D1TestCase):
    def test_english_live_snapshot_is_not_served_in_german(self):
        prompt = self.really_published()
        live_slug = self.live_slug(prompt)
        # a German *draft* translation exists but was never published
        refetch(prompt).create_translation(
            "de", title="DE Entwurf", intro="i", body="DE Text", outro="o",
            slug=f"d1-de-{next(_counter)}",
        )
        self.admin_edit(prompt, title=DRAFT_TITLE)

        self.assertIn(
            prompt.pk, list(Prompt.objects.visible_in_language("en").values_list("pk", flat=True))
        )
        self.assertNotIn(
            prompt.pk, list(Prompt.objects.visible_in_language("de").values_list("pk", flat=True))
        )

        en = self.public.get(f"/en/prompts/{live_slug}/")
        self.assertEqual(en.status_code, 200)
        de = self.public.get(f"/de/prompts/{live_slug}/")
        self.assertEqual(de.status_code, 404)
