"""
Beta 11.13D1G-a: the editorial rework loop must be completable in the
workspace, for every editorial type, without touching the Django admin.

What was broken (Beta 11.13D1A)
-------------------------------
The workspace offered the ``rework`` action but nothing else the loop needs:

* the review queue had no field for a reason, so ``request_rework`` always ran
  with ``note=""`` - and because ``EditorialWorkflowMixin.request_rework()``
  assigns ``self.review_note = note`` *unconditionally*, that actively wiped
  any reason already on the row;
* ``my_content`` never rendered ``review_note``, so an author could not see
  why their content came back;
* a ``rework`` row rendered a ``<form>`` containing no button at all, so there
  was no way to submit again - even though ``move_to_review`` accepts
  ``rework`` as a source state and the endpoint would have taken it.

The loop pinned here is therefore: editor gives a reason -> author reads it ->
author submits again, all inside ``/en/editorial/`` and ``/de/editorial/``.

Scope
-----
No new route, status or domain action. Resubmission reuses the existing
``submit_to_review`` endpoint and the shared Beta 11.13D1B primitive; the
reason is passed to that same primitive rather than written to the model here.
"""
import itertools
import re

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import translation
from reversion.models import Revision

from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin as EW
from guides.models import Guide
from prompts.models import Prompt
from usecases.models import UseCase

User = get_user_model()

_counter = itertools.count()
PASSWORD = "pw-rework"

MODEL_BY_KEY = {
    "guide": Guide,
    "prompt": Prompt,
    "usecase": UseCase,
    "comparison": Comparison,
}

REASON = "Please expand the introduction and add a source."


def _unique(prefix):
    return f"{prefix}-{next(_counter)}"


def make_object(model, *, author, languages=("en", "de")):
    translated = {f.name for f in model._parler_meta.root_model._meta.fields}
    with translation.override("en"):
        obj = model.objects.create(author=author)
        for language in languages:
            values = {"title": f"T {_unique(language)}", "slug": _unique("slug")}
            for optional in ("intro", "body", "outro", "persona"):
                if optional in translated:
                    values[optional] = f"{optional}-text"
            obj.create_translation(language, **values)
    return model.objects.get(pk=obj.pk)


def row_for(html, pk):
    """The ``<tr>`` of one object in a workspace table, or ``None``."""
    match = re.search(
        rf"<tr>(?:(?!</tr>).)*?value=\"{pk}\"(?:(?!</tr>).)*?</tr>", html, re.DOTALL
    )
    return match.group(0) if match else None


def buttons_in(fragment):
    return [
        re.sub(r"<[^>]+>", "", b).strip()
        for b in re.findall(r"<button[^>]*>(.*?)</button>", fragment or "", re.DOTALL)
    ]


class ReworkLoopBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user(username="rework-author", password=PASSWORD)
        cls.author.groups.add(Group.objects.get(name="Author"))
        cls.other_author = User.objects.create_user(
            username="rework-other-author", password=PASSWORD
        )
        cls.other_author.groups.add(Group.objects.get(name="Author"))
        cls.editor = User.objects.create_user(username="rework-editor", password=PASSWORD)
        cls.editor.groups.add(Group.objects.get(name="Editor"))
        cls.plain = User.objects.create_user(username="rework-plain", password=PASSWORD)

    # --- surface drivers ---------------------------------------------------

    def client_for(self, user):
        client = self.client_class()
        client.login(username=user.username, password=PASSWORD)
        return client

    def submit(self, user, key, obj, *, follow=True):
        return self.client_for(user).post(
            reverse("content:editorial:submit_to_review"),
            {"model": key, "object_id": obj.pk},
            follow=follow,
        )

    def review_update(self, user, key, obj, status, *, note=None, follow=True):
        data = {"model": key, "object_id": obj.pk, "status": status}
        if note is not None:
            data["review_note"] = note
        return self.client_for(user).post(
            reverse("content:editorial:review_update"), data, follow=follow
        )

    def my_content_html(self, user, language="en"):
        with translation.override(language):
            url = reverse("content:editorial:my_content")
        response = self.client_for(user).get(url)
        translation.activate("en")
        return response.content.decode()

    def review_queue_html(self, user, language="en"):
        with translation.override(language):
            url = reverse("content:editorial:review_queue")
        response = self.client_for(user).get(url)
        translation.activate("en")
        return response.content.decode()

    def in_review(self, key):
        """An object of ``key`` owned by the author, really submitted for
        review through the workspace so Prompt acquires its real bindings."""
        obj = make_object(MODEL_BY_KEY[key], author=self.author)
        self.submit(self.author, key, obj)
        return MODEL_BY_KEY[key].objects.get(pk=obj.pk)


class ReworkLoopPerTypeTests(ReworkLoopBase):
    """The whole loop, once per editorial type."""

    def _assert_loop(self, key):
        model = MODEL_BY_KEY[key]
        obj = self.in_review(key)
        self.assertEqual(obj.status, EW.STATUS_REVIEW)

        # --- editor requests rework with a reason -------------------------
        before = set(Revision.objects.values_list("pk", flat=True))
        self.review_update(self.editor, key, obj, "rework", note=REASON)

        reworked = model.objects.get(pk=obj.pk)
        self.assertEqual(reworked.status, EW.STATUS_REWORK, f"{key}: not moved to rework")
        self.assertEqual(reworked.review_note, REASON, f"{key}: reason was not stored")
        self.assertEqual(reworked.reviewed_by_id, self.editor.pk)

        rework_revisions = list(Revision.objects.exclude(pk__in=before))
        self.assertEqual(
            len(rework_revisions), 1, f"{key}: rework must create exactly one revision"
        )
        revision = rework_revisions[0]
        self.assertEqual(revision.comment, "request_rework", f"{key}: wrong audit comment")
        self.assertEqual(revision.user_id, self.editor.pk, f"{key}: wrong revision user")
        serialized = "".join(v.serialized_data for v in revision.version_set.all())
        self.assertIn(REASON, serialized, f"{key}: reason is not part of the revision")

        # --- author sees the reason and a resubmit button ------------------
        html = self.my_content_html(self.author)
        self.assertIn(REASON, html, f"{key}: author cannot see the rework reason")

        row = row_for(html, obj.pk)
        self.assertIsNotNone(row, f"{key}: no actionable row rendered for a rework item")
        self.assertNotEqual(
            buttons_in(row), [], f"{key}: rework row rendered a form with no button"
        )
        self.assertIn(
            reverse("content:editorial:submit_to_review"),
            row,
            f"{key}: resubmit does not post to the existing submit endpoint",
        )

        # --- author submits again ------------------------------------------
        before_resubmit = set(Revision.objects.values_list("pk", flat=True))
        self.submit(self.author, key, obj)

        resubmitted = model.objects.get(pk=obj.pk)
        self.assertEqual(
            resubmitted.status, EW.STATUS_REVIEW, f"{key}: resubmission did not take"
        )
        resubmit_revisions = list(Revision.objects.exclude(pk__in=before_resubmit))
        self.assertEqual(
            len(resubmit_revisions),
            1,
            f"{key}: resubmission must create exactly one revision",
        )
        self.assertEqual(resubmit_revisions[0].comment, "submit_for_review")
        self.assertEqual(resubmit_revisions[0].user_id, self.author.pk)

        # Prompt keeps its specialised binding contract on the way back in.
        if key == "prompt":
            self.assertIsNotNone(resubmitted.review_revision_id)
            self.assertNotEqual(resubmitted.review_payload_fingerprint, "")


def _install_loop_tests():
    def make(key):
        def test(self):
            self._assert_loop(key)

        test.__name__ = f"test_{key}_full_rework_loop"
        test.__doc__ = (
            f"{key}: editor requests rework with a reason, the author reads it "
            f"in the workspace and submits again."
        )
        return test

    for key in MODEL_BY_KEY:
        method = make(key)
        setattr(ReworkLoopPerTypeTests, method.__name__, method)


_install_loop_tests()


class ReworkReasonValidationTests(ReworkLoopBase):
    """A rework request without a usable reason must change nothing."""

    def _assert_rejected(self, note):
        obj = self.in_review("guide")
        before = Revision.objects.count()

        self.review_update(self.editor, "guide", obj, "rework", note=note)

        unchanged = Guide.objects.get(pk=obj.pk)
        self.assertEqual(unchanged.status, EW.STATUS_REVIEW, "status changed anyway")
        self.assertEqual(unchanged.review_note, "", "review_note was mutated anyway")
        self.assertEqual(Revision.objects.count(), before, "a revision was recorded")

    def test_missing_reason_is_rejected(self):
        obj = self.in_review("guide")
        before = Revision.objects.count()
        self.review_update(self.editor, "guide", obj, "rework")  # no field at all
        self.assertEqual(Guide.objects.get(pk=obj.pk).status, EW.STATUS_REVIEW)
        self.assertEqual(Revision.objects.count(), before)

    def test_empty_reason_is_rejected(self):
        self._assert_rejected("")

    def test_whitespace_only_reason_is_rejected(self):
        self._assert_rejected("   \n\t  ")

    def test_rejection_shows_an_error_and_returns_to_the_review_queue(self):
        obj = self.in_review("guide")
        response = self.review_update(self.editor, "guide", obj, "rework", note="  ")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "content/editorial/review_queue.html")
        self.assertNotContains(response, "Status updated.")

    def test_surrounding_whitespace_is_normalised(self):
        obj = self.in_review("guide")
        self.review_update(self.editor, "guide", obj, "rework", note="  needs work  ")
        self.assertEqual(Guide.objects.get(pk=obj.pk).review_note, "needs work")

    def test_multiline_reason_is_stored_verbatim(self):
        obj = self.in_review("guide")
        reason = "First problem.\n\nSecond problem."
        self.review_update(self.editor, "guide", obj, "rework", note=reason)
        self.assertEqual(Guide.objects.get(pk=obj.pk).review_note, reason)

    def test_reason_is_ignored_for_approve(self):
        obj = self.in_review("guide")
        self.review_update(self.editor, "guide", obj, "approved", note="ignore me")
        approved = Guide.objects.get(pk=obj.pk)
        self.assertEqual(approved.status, EW.STATUS_APPROVED)
        self.assertEqual(approved.review_note, "", "approve must not persist the note")

    def test_reason_is_ignored_for_archive(self):
        obj = self.in_review("guide")
        self.review_update(self.editor, "guide", obj, "archived", note="ignore me too")
        archived = Guide.objects.get(pk=obj.pk)
        self.assertEqual(archived.status, EW.STATUS_ARCHIVED)
        self.assertEqual(archived.review_note, "", "archive must not persist the note")


class ReworkRolesAndOwnershipTests(ReworkLoopBase):
    """The new field must not widen who may do what."""

    def test_author_cannot_request_rework_even_with_a_reason(self):
        obj = self.in_review("guide")
        before = Revision.objects.count()
        self.review_update(self.author, "guide", obj, "rework", note=REASON)
        self.assertEqual(Guide.objects.get(pk=obj.pk).status, EW.STATUS_REVIEW)
        self.assertEqual(Guide.objects.get(pk=obj.pk).review_note, "")
        self.assertEqual(Revision.objects.count(), before)

    def test_plain_user_cannot_reach_review_update(self):
        obj = self.in_review("guide")
        response = self.review_update(self.plain, "guide", obj, "rework", note=REASON)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Guide.objects.get(pk=obj.pk).status, EW.STATUS_REVIEW)

    def test_anonymous_cannot_reach_review_update(self):
        obj = self.in_review("guide")
        response = self.client.post(
            reverse("content:editorial:review_update"),
            {"model": "guide", "object_id": obj.pk, "status": "rework", "review_note": REASON},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Guide.objects.get(pk=obj.pk).status, EW.STATUS_REVIEW)

    def test_editor_cannot_rework_their_own_authored_content(self):
        obj = make_object(Guide, author=self.editor)
        self.submit(self.editor, "guide", obj)
        before = Revision.objects.count()
        self.review_update(self.editor, "guide", obj, "rework", note=REASON)
        self.assertEqual(Guide.objects.get(pk=obj.pk).status, EW.STATUS_REVIEW)
        self.assertEqual(Revision.objects.count(), before)

    def test_author_only_sees_reasons_of_their_own_content(self):
        foreign = make_object(Guide, author=self.other_author)
        self.submit(self.other_author, "guide", foreign)
        secret = "Foreign reason that must stay hidden."
        self.review_update(self.editor, "guide", foreign, "rework", note=secret)

        html = self.my_content_html(self.author)
        self.assertNotIn(secret, html)
        self.assertIsNone(row_for(html, foreign.pk))

    def test_unknown_object_id_stays_fail_closed(self):
        response = self.review_update(
            self.editor, "guide", type("O", (), {"pk": 9_999_999})(), "rework",
            note=REASON, follow=False,
        )
        self.assertEqual(response.status_code, 404)

    def test_unknown_model_key_stays_fail_closed(self):
        obj = self.in_review("guide")
        response = self.client_for(self.editor).post(
            reverse("content:editorial:review_update"),
            {"model": "tool", "object_id": obj.pk, "status": "rework", "review_note": REASON},
            follow=True,
        )
        self.assertContains(response, "Invalid request.")
        self.assertEqual(Guide.objects.get(pk=obj.pk).status, EW.STATUS_REVIEW)

    def test_author_cannot_resubmit_someone_elses_rework_item(self):
        foreign = make_object(Guide, author=self.other_author)
        self.submit(self.other_author, "guide", foreign)
        self.review_update(self.editor, "guide", foreign, "rework", note=REASON)

        before = Revision.objects.count()
        self.submit(self.author, "guide", foreign)
        self.assertEqual(Guide.objects.get(pk=foreign.pk).status, EW.STATUS_REWORK)
        self.assertEqual(Revision.objects.count(), before)


class ReworkReasonEscapingTests(ReworkLoopBase):
    """The reason is plain text and must never become markup."""

    PAYLOAD = "<script>alert(1)</script><strong>change this</strong>"

    def test_reason_is_stored_verbatim_and_rendered_escaped(self):
        obj = self.in_review("guide")
        self.review_update(self.editor, "guide", obj, "rework", note=self.PAYLOAD)

        self.assertEqual(Guide.objects.get(pk=obj.pk).review_note, self.PAYLOAD)

        html = self.my_content_html(self.author)
        self.assertNotIn(self.PAYLOAD, html, "the raw payload reached the page")
        self.assertNotIn(
            "<strong>change this</strong>", html, "input tags became real markup"
        )
        self.assertIn("&lt;script&gt;", html, "the payload was not escaped")

    def test_multiline_reason_renders_readably_without_mark_safe(self):
        obj = self.in_review("guide")
        self.review_update(
            self.editor, "guide", obj, "rework", note="line one\nline two"
        )
        html = self.my_content_html(self.author)
        self.assertIn("line one", html)
        self.assertIn("line two", html)


class ReworkSurfaceTests(ReworkLoopBase):
    """What the two workspace pages actually render."""

    def test_draft_keeps_the_plain_submit_label(self):
        obj = make_object(Guide, author=self.author)
        row = row_for(self.my_content_html(self.author), obj.pk)
        self.assertIn("Submit for review", row)
        self.assertNotIn("Submit again for review", row)

    def test_rework_row_offers_the_resubmit_action(self):
        obj = self.in_review("guide")
        self.review_update(self.editor, "guide", obj, "rework", note=REASON)
        row = row_for(self.my_content_html(self.author), obj.pk)
        self.assertIn("Submit again for review", row)
        self.assertIn(reverse("content:editorial:submit_to_review"), row)
        self.assertIn("csrfmiddlewaretoken", row)
        self.assertIn('method="post"', row)

    def test_review_queue_offers_a_labelled_reason_field(self):
        obj = self.in_review("guide")
        html = self.review_queue_html(self.editor)
        self.assertIn('name="review_note"', html)
        field_id = f"review-note-guide-{obj.pk}"
        self.assertIn(f'id="{field_id}"', html)
        self.assertIn(f'for="{field_id}"', html)

    def test_review_queue_field_ids_are_unique_per_row(self):
        first = self.in_review("guide")
        second = self.in_review("guide")
        html = self.review_queue_html(self.editor)
        ids = re.findall(r'id="(review-note-[^"]+)"', html)
        self.assertEqual(len(ids), len(set(ids)), f"duplicate field ids: {ids}")
        self.assertIn(f"review-note-guide-{first.pk}", ids)
        self.assertIn(f"review-note-guide-{second.pk}", ids)

    def test_no_editorial_row_renders_an_empty_form(self):
        statuses = ("draft", "rework", "approved", "published", "archived")
        objects = {}
        for status in statuses:
            obj = make_object(Guide, author=self.author)
            if status == "rework":
                self.submit(self.author, "guide", obj)
                self.review_update(self.editor, "guide", obj, "rework", note=REASON)
            elif status != "draft":
                Guide.objects.filter(pk=obj.pk).update(status=status)
            objects[status] = obj

        html = self.my_content_html(self.author)
        for status, obj in objects.items():
            row = row_for(html, obj.pk)
            if row is None:
                continue
            self.assertNotEqual(
                buttons_in(row), [], f"{status}: row renders a form with no button"
            )


class ReworkLanguageParityTests(ReworkLoopBase):
    """EN and DE must offer the same loop in their own language."""

    def test_table_headers_are_english_on_en(self):
        html = self.my_content_html(self.author, "en")
        headers = re.findall(r"<th[^>]*>([^<]*)</th>", html)
        headers = [h.strip() for h in headers if h.strip()]
        self.assertIn("Type", headers)
        self.assertIn("Title", headers)
        self.assertNotIn("Typ", headers)
        self.assertNotIn("Titel", headers)

    def test_table_headers_are_german_on_de(self):
        html = self.my_content_html(self.author, "de")
        headers = re.findall(r"<th[^>]*>([^<]*)</th>", html)
        headers = [h.strip() for h in headers if h.strip()]
        self.assertIn("Typ", headers)
        self.assertIn("Titel", headers)

    def test_richtext_layouts_link_is_translated(self):
        en = self.my_content_html(self.author, "en")
        de = self.my_content_html(self.author, "de")
        self.assertIn("Rich text layouts", en)
        self.assertNotIn("Rich text layouts", de)

    def test_resubmit_button_is_translated(self):
        obj = self.in_review("guide")
        self.review_update(self.editor, "guide", obj, "rework", note=REASON)
        en_row = row_for(self.my_content_html(self.author, "en"), obj.pk)
        de_row = row_for(self.my_content_html(self.author, "de"), obj.pk)
        self.assertIn("Submit again for review", en_row)
        self.assertNotIn("Submit again for review", de_row)
        self.assertNotEqual(buttons_in(de_row), [])

    def test_review_queue_reason_field_is_translated(self):
        self.in_review("guide")
        en = self.review_queue_html(self.editor, "en")
        de = self.review_queue_html(self.editor, "de")
        self.assertIn("Reason for rework", en)
        self.assertNotIn("Reason for rework", de)
        self.assertIn('name="review_note"', de)

    def test_the_reason_itself_is_never_translated(self):
        obj = self.in_review("guide")
        self.review_update(self.editor, "guide", obj, "rework", note=REASON)
        for language in ("en", "de"):
            self.assertIn(REASON, self.my_content_html(self.author, language))

    def test_both_languages_keep_their_prefix_through_the_loop(self):
        for language in ("en", "de"):
            with self.subTest(language=language):
                obj = self.in_review("guide")
                with translation.override(language):
                    url = reverse("content:editorial:review_update")
                    expected = reverse("content:editorial:review_queue")
                response = self.client_for(self.editor).post(
                    url,
                    {
                        "model": "guide",
                        "object_id": obj.pk,
                        "status": "rework",
                        "review_note": REASON,
                    },
                )
                translation.activate("en")
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["Location"], expected)
                self.assertTrue(expected.startswith(f"/{language}/"))


class ReworkSharedPrimitiveTests(ReworkLoopBase):
    """The loop must run through the D1B primitive, not a local mutation."""

    def test_rework_and_resubmit_both_go_through_the_shared_action(self):
        from unittest import mock

        obj = self.in_review("guide")

        with mock.patch(
            "content.views.editorial.apply_editorial_action",
            wraps=__import__(
                "core.editorial_actions", fromlist=["apply_editorial_action"]
            ).apply_editorial_action,
        ) as shared:
            self.review_update(self.editor, "guide", obj, "rework", note=REASON)
            self.submit(self.author, "guide", obj)

        actions = [call.args[1] for call in shared.call_args_list]
        self.assertEqual(len(actions), 2, "the loop did not use the shared primitive twice")
        notes = [call.kwargs.get("note") for call in shared.call_args_list]
        self.assertEqual(notes[0], REASON, "the reason was not handed to the primitive")
        self.assertIsNone(
            notes[1], "resubmission must not override the primitive's canonical note"
        )

    def test_resubmission_preserves_the_stored_reason(self):
        """
        ``move_to_review`` assigns ``review_note`` only when a note is given,
        and the shared primitive deliberately keeps ``review_note`` out of the
        submit action's ``update_fields``. The reason therefore survives the
        resubmission and stays available as the last editorial note - D1G-a
        adds no clearing of its own.
        """
        obj = self.in_review("guide")
        self.review_update(self.editor, "guide", obj, "rework", note=REASON)
        self.submit(self.author, "guide", obj)

        resubmitted = Guide.objects.get(pk=obj.pk)
        self.assertEqual(resubmitted.status, EW.STATUS_REVIEW)
        self.assertEqual(resubmitted.review_note, REASON)
