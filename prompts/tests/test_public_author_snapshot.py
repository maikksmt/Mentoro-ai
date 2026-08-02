"""
Beta 11.11C4F: the public Prompt author display, rendered exclusively from
the Beta 11.11C4E publish-time ``live_author`` snapshot.

Three public surfaces are under test - the detail page's visible byline, its
``<meta name="author">``, and the prompt-list card's author line - all
routed through the single resolver in ``prompts/live_author.py``. These
tests hold the whole chain to the C4F contract: a valid snapshot renders
unchanged and HTML-escaped; an empty, missing, or malformed snapshot hides
the author everywhere with no exception and no fallback to the live
``Prompt.author`` relation; later name/username/author/account-deletion
changes never move any of the three surfaces; a conscious republish updates
all three at once; draft preview and other editorial types are completely
unaffected.
"""
import itertools

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin as Workflow
from guides.models import Guide
from prompts.live_author import resolve_prompt_live_author_display_name
from prompts.models import PROMPT_AUTHOR_SNAPSHOT_SCHEMA, Prompt
from prompts.tests.draft_preview_fixtures import make_draft_prompt, make_user
from prompts.tests.draft_preview_fixtures import publish as publish_via_workflow
from usecases.models import UseCase

User = get_user_model()

_slug_counter = itertools.count()


def refetch(prompt):
    """django-fsm's protected ``status`` descriptor rejects the ``setattr``
    ``refresh_from_db()`` performs, so reload through the manager."""
    return Prompt.objects.get(pk=prompt.pk)


def make_prompt(*, author=None, languages=("en",)):
    prompt = Prompt.objects.create(author=author)
    for language_code in languages:
        prompt.create_translation(
            language_code,
            title=f"Title {language_code}",
            intro="intro",
            body="body",
            outro="outro",
            slug=f"pub-author-slug-{next(_slug_counter)}",
        )
    return prompt


def full_cycle_to_published(prompt, actor):
    return publish_via_workflow(prompt, actor)


def detail_url(prompt):
    prompt = refetch(prompt)
    prompt.set_current_language("en")
    return reverse("prompts:detail", kwargs={"slug": prompt.slug})


# ======================================================================
# Phase 4: the resolver itself, function-level
# ======================================================================


class _Snapshot:
    """A minimal stand-in for a Prompt exposing only ``live_author`` - the
    resolver's stated contract is that it reads nothing else."""

    def __init__(self, live_author):
        self.live_author = live_author


class ResolverContractTests(TestCase):
    def _resolve(self, live_author):
        return resolve_prompt_live_author_display_name(_Snapshot(live_author))

    def test_valid_snapshot_returns_the_display_name(self):
        self.assertEqual(
            self._resolve({"schema": "prompt-author-v1", "display_name": "Jane Doe"}), "Jane Doe"
        )

    def test_special_characters_are_returned_unchanged(self):
        for name in ("Jürgen Müller-Groß", "O'Brien", "田中 太郎", "Åsa Öberg"):
            with self.subTest(name=name):
                self.assertEqual(
                    self._resolve({"schema": "prompt-author-v1", "display_name": name}), name
                )

    def test_explicit_empty_display_name_returns_empty_string(self):
        self.assertEqual(
            self._resolve({"schema": "prompt-author-v1", "display_name": ""}), ""
        )

    def test_whitespace_only_display_name_returns_empty_string(self):
        self.assertEqual(
            self._resolve({"schema": "prompt-author-v1", "display_name": "   "}), ""
        )

    def test_missing_snapshot_returns_empty_string(self):
        self.assertEqual(self._resolve(None), "")

    def test_empty_dict_returns_empty_string(self):
        self.assertEqual(self._resolve({}), "")

    def test_missing_schema_key_returns_empty_string(self):
        self.assertEqual(self._resolve({"display_name": "Jane"}), "")

    def test_missing_display_name_key_returns_empty_string(self):
        self.assertEqual(self._resolve({"schema": "prompt-author-v1"}), "")

    def test_wrong_schema_returns_empty_string(self):
        self.assertEqual(
            self._resolve({"schema": "prompt-author-v2", "display_name": "Jane"}), ""
        )

    def test_snapshot_as_list_returns_empty_string(self):
        self.assertEqual(self._resolve(["schema", "prompt-author-v1"]), "")

    def test_snapshot_as_string_returns_empty_string(self):
        self.assertEqual(self._resolve("prompt-author-v1"), "")

    def test_snapshot_as_number_returns_empty_string(self):
        self.assertEqual(self._resolve(42), "")

    def test_display_name_none_returns_empty_string(self):
        self.assertEqual(self._resolve({"schema": "prompt-author-v1", "display_name": None}), "")

    def test_display_name_number_returns_empty_string(self):
        self.assertEqual(self._resolve({"schema": "prompt-author-v1", "display_name": 5}), "")

    def test_display_name_list_returns_empty_string(self):
        self.assertEqual(self._resolve({"schema": "prompt-author-v1", "display_name": ["Jane"]}), "")

    def test_display_name_dict_returns_empty_string(self):
        self.assertEqual(
            self._resolve({"schema": "prompt-author-v1", "display_name": {"a": "b"}}), ""
        )

    def test_unknown_extra_fields_are_ignored_when_otherwise_valid(self):
        self.assertEqual(
            self._resolve(
                {"schema": "prompt-author-v1", "display_name": "Jane", "extra": "ignored", "id": 5}
            ),
            "Jane",
        )

    def test_confirmed_against_the_real_schema_constant(self):
        self.assertEqual(PROMPT_AUTHOR_SNAPSHOT_SCHEMA, "prompt-author-v1")

    def test_never_reads_the_author_relation_even_when_one_exists(self):
        author = User.objects.create_user("resolver-noquery-author", password="pw")
        prompt = make_prompt(author=author)
        self.assertIsNone(refetch(prompt).live_author)

        with CaptureQueriesContext(connection) as ctx:
            result = resolve_prompt_live_author_display_name(refetch(prompt))

        self.assertEqual(result, "")
        auth_queries = [q for q in ctx.captured_queries if '"auth_user"' in q["sql"]]
        self.assertEqual(auth_queries, [])


# ======================================================================
# Phase 11: the detail page
# ======================================================================


class DetailPageBylineTests(TestCase):
    def test_valid_snapshot_shows_the_byline_and_meta(self):
        author = User.objects.create_user(
            "detail-valid-author", password="pw", first_name="Jane", last_name="Doe"
        )
        prompt = full_cycle_to_published(make_prompt(author=author), author)
        resp = self.client.get(detail_url(prompt))
        html = resp.content.decode()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Jane Doe", html)
        self.assertIn('<meta name="author" content="Jane Doe">', html)

    def test_empty_snapshot_hides_byline_and_meta(self):
        actor = User.objects.create_user("detail-empty-actor", password="pw")
        prompt = full_cycle_to_published(make_prompt(author=None), actor)
        self.assertEqual(refetch(prompt).live_author, {"schema": "prompt-author-v1", "display_name": ""})
        resp = self.client.get(detail_url(prompt))
        html = resp.content.decode()
        self.assertNotIn("Author:", html)
        self.assertNotIn('meta name="author"', html)

    def test_missing_snapshot_hides_byline_and_meta_without_error(self):
        """Simulates a legacy row published before Beta 11.11C4E existed: a
        real live_i18n content snapshot, but no live_author at all."""
        actor = User.objects.create_user("detail-missing-actor", password="pw")
        prompt = full_cycle_to_published(make_prompt(author=actor), actor)
        url = detail_url(prompt)
        Prompt.objects.filter(pk=prompt.pk).update(live_author=None)
        self.assertIsNone(refetch(prompt).live_author)
        resp = self.client.get(url)
        html = resp.content.decode()
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("Author:", html)
        self.assertNotIn('meta name="author"', html)

    def test_malformed_snapshot_hides_byline_and_meta_without_error(self):
        actor = User.objects.create_user("detail-malformed-actor", password="pw")
        prompt = full_cycle_to_published(make_prompt(author=actor), actor)
        Prompt.objects.filter(pk=prompt.pk).update(live_author={"unexpected": "shape"})
        resp = self.client.get(detail_url(prompt))
        html = resp.content.decode()
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("Author:", html)
        self.assertNotIn('meta name="author"', html)

    def test_first_name_change_after_publish_does_not_affect_the_page(self):
        author = User.objects.create_user("detail-stable-first", password="pw", first_name="Old")
        prompt = full_cycle_to_published(make_prompt(author=author), author)
        url = detail_url(prompt)
        User.objects.filter(pk=author.pk).update(first_name="New")
        html = self.client.get(url).content.decode()
        self.assertIn("Old", html)
        self.assertNotIn(">New<", html)

    def test_username_change_after_publish_does_not_affect_the_page(self):
        author = User.objects.create_user("detail-stable-username-old", password="pw")
        prompt = full_cycle_to_published(make_prompt(author=author), author)
        url = detail_url(prompt)
        User.objects.filter(pk=author.pk).update(username="detail-stable-username-new")
        html = self.client.get(url).content.decode()
        self.assertIn("detail-stable-username-old", html)
        self.assertNotIn("detail-stable-username-new", html)

    def test_author_reassignment_after_publish_does_not_affect_the_page(self):
        author_a = User.objects.create_user("detail-stable-a", password="pw", first_name="Wandaperson")
        author_b = User.objects.create_user("detail-stable-b", password="pw", first_name="Victorperson")
        prompt = full_cycle_to_published(make_prompt(author=author_a), author_a)
        url = detail_url(prompt)
        Prompt.objects.filter(pk=prompt.pk).update(author=author_b)
        html = self.client.get(url).content.decode()
        self.assertIn("Wandaperson", html)
        self.assertNotIn("Victorperson", html)

    def test_user_deletion_keeps_the_historical_byline_and_meta(self):
        author = User.objects.create_user("detail-deleted", password="pw", first_name="Gone", last_name="Person")
        prompt = full_cycle_to_published(make_prompt(author=author), author)
        url = detail_url(prompt)
        author.delete()
        reloaded = refetch(prompt)
        self.assertIsNone(reloaded.author_id)

        resp = self.client.get(url)
        html = resp.content.decode()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Gone Person", html)
        self.assertIn('<meta name="author" content="Gone Person">', html)

    def test_html_in_display_name_is_escaped_not_executed(self):
        author = User.objects.create_user(
            "detail-xss-author", password="pw", first_name="<script>alert(1)</script>"
        )
        prompt = full_cycle_to_published(make_prompt(author=author), author)
        html = self.client.get(detail_url(prompt)).content.decode()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)


# ======================================================================
# Phase 7: SEO meta specifically
# ======================================================================


class SeoMetaTests(TestCase):
    def test_exactly_one_author_meta_when_valid(self):
        author = User.objects.create_user("seo-valid-author", password="pw", first_name="Ada")
        prompt = full_cycle_to_published(make_prompt(author=author), author)
        html = self.client.get(detail_url(prompt)).content.decode()
        self.assertEqual(html.count('name="author"'), 1)

    def test_no_author_meta_when_empty(self):
        actor = User.objects.create_user("seo-empty-actor", password="pw")
        prompt = full_cycle_to_published(make_prompt(author=None), actor)
        html = self.client.get(detail_url(prompt)).content.decode()
        self.assertEqual(html.count('name="author"'), 0)

    def test_no_author_meta_when_malformed(self):
        actor = User.objects.create_user("seo-malformed-actor", password="pw")
        prompt = full_cycle_to_published(make_prompt(author=actor), actor)
        Prompt.objects.filter(pk=prompt.pk).update(live_author=["not", "a", "dict"])
        html = self.client.get(detail_url(prompt)).content.decode()
        self.assertEqual(html.count('name="author"'), 0)

    def test_special_characters_in_meta_content_are_escaped(self):
        author = User.objects.create_user(
            "seo-escape-author", password="pw", first_name='"><script>x</script>'
        )
        prompt = full_cycle_to_published(make_prompt(author=author), author)
        html = self.client.get(detail_url(prompt)).content.decode()
        self.assertNotIn('content=""><script>x</script>"', html)
        self.assertNotIn("<script>x</script>", html)


# ======================================================================
# Phase 10: prompt-list cards
# ======================================================================


class ListCardTests(TestCase):
    def _list_html(self):
        resp = self.client.get(reverse("prompts:list"))
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_valid_snapshot_is_shown_on_the_card(self):
        author = User.objects.create_user(
            "card-valid-author", password="pw", first_name="Grace", last_name="Hopper"
        )
        full_cycle_to_published(make_prompt(author=author), author)
        html = self._list_html()
        self.assertIn("Grace Hopper", html)

    def test_current_name_divergence_does_not_leak_onto_the_card(self):
        author = User.objects.create_user("card-diverge-author", password="pw", first_name="Old")
        full_cycle_to_published(make_prompt(author=author), author)
        User.objects.filter(pk=author.pk).update(first_name="New")
        html = self._list_html()
        self.assertIn("Old", html)
        self.assertNotIn(">New<", html)

    def test_username_change_does_not_affect_the_card(self):
        author = User.objects.create_user("card-username-old", password="pw")
        full_cycle_to_published(make_prompt(author=author), author)
        User.objects.filter(pk=author.pk).update(username="card-username-new")
        html = self._list_html()
        self.assertIn("card-username-old", html)
        self.assertNotIn("card-username-new", html)

    def test_author_reassignment_does_not_affect_the_card(self):
        author_a = User.objects.create_user("card-reassign-a", password="pw", first_name="Wandaperson")
        author_b = User.objects.create_user("card-reassign-b", password="pw", first_name="Victorperson")
        prompt = full_cycle_to_published(make_prompt(author=author_a), author_a)
        Prompt.objects.filter(pk=prompt.pk).update(author=author_b)
        html = self._list_html()
        self.assertIn("Wandaperson", html)
        self.assertNotIn("Victorperson", html)

    def test_user_deletion_keeps_the_card_name(self):
        author = User.objects.create_user("card-deleted", password="pw", first_name="Deleted", last_name="User")
        full_cycle_to_published(make_prompt(author=author), author)
        author.delete()
        html = self._list_html()
        self.assertIn("Deleted User", html)

    def test_empty_snapshot_shows_no_author_line_or_stray_separator(self):
        actor = User.objects.create_user("card-empty-actor", password="pw")
        full_cycle_to_published(make_prompt(author=None), actor)
        html = self._list_html()
        self.assertNotIn(">from <", html)

    def test_missing_snapshot_shows_no_author_line(self):
        actor = User.objects.create_user("card-missing-actor", password="pw")
        prompt = full_cycle_to_published(make_prompt(author=actor), actor)
        Prompt.objects.filter(pk=prompt.pk).update(live_author=None)
        html = self._list_html()
        self.assertNotIn(">from <", html)

    def test_malformed_snapshot_shows_no_author_line_and_no_error(self):
        actor = User.objects.create_user("card-malformed-actor", password="pw")
        prompt = full_cycle_to_published(make_prompt(author=actor), actor)
        Prompt.objects.filter(pk=prompt.pk).update(live_author={"schema": "prompt-author-v1"})
        resp = self.client.get(reverse("prompts:list"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(">from <", resp.content.decode())

    def test_html_in_card_name_is_escaped(self):
        author = User.objects.create_user(
            "card-xss-author", password="pw", first_name="<b>bold</b>"
        )
        full_cycle_to_published(make_prompt(author=author), author)
        html = self._list_html()
        self.assertNotIn("<b>bold</b>", html)
        self.assertIn("&lt;b&gt;bold&lt;/b&gt;", html)


# ======================================================================
# Phase 12/13: republish and user-deletion end-to-end
# ======================================================================


class RepublishEndToEndTests(TestCase):
    def test_republish_updates_all_three_public_surfaces(self):
        author_a = User.objects.create_user("e2e-republish-a", password="pw", first_name="First")
        author_b = User.objects.create_user("e2e-republish-b", password="pw", first_name="Second")

        prompt = full_cycle_to_published(make_prompt(author=author_a), author_a)
        url = detail_url(prompt)

        detail_before = self.client.get(url).content.decode()
        list_before = self.client.get(reverse("prompts:list")).content.decode()
        self.assertIn("First", detail_before)
        self.assertIn("First", list_before)

        Prompt.objects.filter(pk=prompt.pk).update(author=author_b)

        # Without republish, nothing changes.
        detail_unchanged = self.client.get(url).content.decode()
        self.assertIn("First", detail_unchanged)
        self.assertNotIn("Second", detail_unchanged)

        # A real republish cycle: review -> approve -> publish again.
        prompt = refetch(prompt)
        prompt.move_to_review(by=author_b)
        prompt.save()
        prompt = refetch(prompt)
        prompt.approve(by=author_b)
        prompt.save()
        prompt = refetch(prompt)
        prompt.publish(by=author_b)
        prompt.save()

        detail_after = self.client.get(url).content.decode()
        list_after = self.client.get(reverse("prompts:list")).content.decode()
        self.assertIn("Second", detail_after)
        self.assertNotIn("First", detail_after)
        self.assertIn("Second", list_after)
        self.assertIn('<meta name="author" content="Second">', detail_after)


class UserDeletionEndToEndTests(TestCase):
    def test_deletion_end_to_end(self):
        author = User.objects.create_user(
            "e2e-deletion-author", password="pw", first_name="Historical", last_name="Author"
        )
        prompt = full_cycle_to_published(make_prompt(author=author), author)
        self.assertEqual(refetch(prompt).live_author["display_name"], "Historical Author")

        url = detail_url(prompt)
        author.delete()
        self.assertIsNone(refetch(prompt).author_id)

        with CaptureQueriesContext(connection) as ctx:
            detail_resp = self.client.get(url)
        self.assertEqual(detail_resp.status_code, 200)
        self.assertIn("Historical Author", detail_resp.content.decode())
        auth_queries = [q for q in ctx.captured_queries if '"auth_user"' in q["sql"]]
        self.assertEqual(auth_queries, [])

        list_html = self.client.get(reverse("prompts:list")).content.decode()
        self.assertIn("Historical Author", list_html)


# ======================================================================
# Phase 14/18: no live dependency, query contract
# ======================================================================


class NoLiveDependencyTests(TestCase):
    def test_detail_query_capture_has_no_auth_user_select(self):
        author = User.objects.create_user("query-detail-author", password="pw")
        prompt = full_cycle_to_published(make_prompt(author=author), author)
        url = detail_url(prompt)
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(url)
        auth_queries = [q for q in ctx.captured_queries if '"auth_user"' in q["sql"]]
        self.assertEqual(auth_queries, [])

    def test_list_query_capture_has_no_auth_user_select(self):
        author = User.objects.create_user("query-list-author", password="pw")
        full_cycle_to_published(make_prompt(author=author), author)
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(reverse("prompts:list"))
        auth_queries = [q for q in ctx.captured_queries if '"auth_user"' in q["sql"]]
        self.assertEqual(auth_queries, [])

    def test_no_live_author_relation_access_remains_in_prompts_views(self):
        import prompts.views as views_module

        with open(views_module.__file__, encoding="utf-8") as _f:
            source = _f.read()
        for forbidden in ("author_obj.get_full_name", "author_obj.username", ".author.get_full_name"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_no_live_author_relation_access_remains_in_the_public_branch(self):
        """The template's ``{% if is_preview %}`` branch is explicitly
        allowed to keep reading ``object.author`` (Beta 11.11C4F Phase 15:
        draft preview stays live) - only the public ``{% elif
        author_display_name %}`` branch, and prompt_list.html (which never
        had an is_preview concept), must be free of it."""
        import pathlib

        detail_text = pathlib.Path("templates/prompts/prompt_detail.html").read_text(encoding="utf-8")
        public_branch = detail_text.split("{% elif author_display_name %}")[1].split("{% endif %}")[0]
        self.assertNotIn("object.author", public_branch)

        list_text = pathlib.Path("templates/prompts/prompt_list.html").read_text(encoding="utf-8")
        self.assertNotIn("object.author.get_full_name", list_text)
        self.assertNotIn("object.author.username", list_text)


# ======================================================================
# Phase 15: draft preview and other internal surfaces stay live
# ======================================================================


class DraftPreviewUnaffectedTests(TestCase):
    def setUp(self):
        self.editor = make_user("preview-unaffected-editor", group="Editor")

    def test_preview_still_shows_the_current_live_author(self):
        prompt = make_draft_prompt(
            self.editor, slug="preview-live-en", title="Preview Live"
        )
        self.client.force_login(self.editor)
        preview_url = reverse("admin:prompts_prompt_draft_preview", args=[prompt.pk, "en"])
        resp = self.client.get(preview_url)
        html = resp.content.decode()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("preview-unaffected-editor", html)

    def test_preview_works_for_a_prompt_with_no_live_author_at_all(self):
        prompt = make_draft_prompt(
            self.editor, slug="preview-nolive-en", title="Preview No Live"
        )
        self.assertIsNone(prompt.live_author)
        self.client.force_login(self.editor)
        preview_url = reverse("admin:prompts_prompt_draft_preview", args=[prompt.pk, "en"])
        resp = self.client.get(preview_url)
        self.assertEqual(resp.status_code, 200)


# ======================================================================
# Phase 16: other editorial types are untouched
# ======================================================================


class OtherEditorialTypesUnaffectedTests(TestCase):
    def test_guide_card_still_shows_the_live_author(self):
        author = User.objects.create_user("other-guide-author", password="pw", first_name="Guide", last_name="Person")
        guide = Guide.objects.create(
            author=author, status=Workflow.STATUS_PUBLISHED, published_at=timezone.now()
        )
        guide.create_translation("en", title="Other Guide", intro="i", body="b", slug="other-guide-en")
        html = self.client.get(reverse("guides:list")).content.decode()
        self.assertIn("Guide Person", html)

    def test_usecase_card_still_shows_the_live_author(self):
        author = User.objects.create_user("other-uc-author", password="pw", first_name="Case", last_name="Person")
        usecase = UseCase.objects.create(
            author=author, status=Workflow.STATUS_PUBLISHED, published_at=timezone.now()
        )
        usecase.create_translation(
            "en", title="Other UseCase", intro="i", body="b", outro="o", persona="p", slug="other-uc-en"
        )
        html = self.client.get(reverse("usecases:list")).content.decode()
        self.assertIn("Case Person", html)

    def test_comparison_card_still_shows_the_live_author(self):
        author = User.objects.create_user("other-cmp-author", password="pw", first_name="Compare", last_name="Person")
        comparison = Comparison.objects.create(
            author=author, status=Workflow.STATUS_PUBLISHED, published_at=timezone.now()
        )
        comparison.create_translation("en", title="Other Comparison", intro="i", body="b", slug="other-cmp-en")
        html = self.client.get(reverse("compare:index")).content.decode()
        self.assertIn("Compare Person", html)

    def test_other_editorial_types_have_no_live_author_attribute(self):
        for model in (Guide, UseCase, Comparison):
            with self.subTest(model=model.__name__):
                self.assertFalse(hasattr(model, "live_author"))

    def test_editorial_card_partial_default_path_needs_no_snapshot_flag(self):
        """Guide/UseCase/Comparison's existing includes never pass
        author_uses_snapshot at all - confirmed by grepping their real
        templates rather than assuming it."""
        import pathlib

        for path in (
            "templates/guides/guide_list.html",
            "templates/usecases/list.html",
            "templates/compare/comparison_list.html",
        ):
            text = pathlib.Path(path).read_text(encoding="utf-8")
            with self.subTest(path=path):
                self.assertNotIn("author_uses_snapshot", text)


# ======================================================================
# Phase 19: rendering never writes anything
# ======================================================================


class NoWorkflowSideEffectTests(TestCase):
    def test_a_public_get_request_does_not_mutate_the_prompt(self):
        author = User.objects.create_user("noeffect-author", password="pw")
        prompt = full_cycle_to_published(make_prompt(author=author), author)
        before = refetch(prompt)
        before_snapshot = {
            "updated_at": before.updated_at,
            "live_author": before.live_author,
            "status": before.status,
        }

        self.client.get(detail_url(prompt))
        self.client.get(reverse("prompts:list"))

        after = refetch(prompt)
        self.assertEqual(after.updated_at, before_snapshot["updated_at"])
        self.assertEqual(after.live_author, before_snapshot["live_author"])
        self.assertEqual(after.status, before_snapshot["status"])


# ======================================================================
# Phase 25 (subset): static security checks
# ======================================================================


class StaticSecurityTests(TestCase):
    def test_resolver_module_has_no_mark_safe_or_broad_exceptions(self):
        import pathlib

        text = pathlib.Path("prompts/live_author.py").read_text(encoding="utf-8")
        for forbidden in ("mark_safe", "format_html", "except Exception", "pragma: no cover", "skip", "xfail"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)

    def test_resolver_never_reads_the_author_relation(self):
        """AST-based, not a substring search: the module's own docstring
        legitimately mentions ``prompt.author``/``author_id`` in prose to
        explain what it deliberately does NOT do - only actual attribute
        access in code must be free of them."""
        import ast
        import pathlib

        source = pathlib.Path("prompts/live_author.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders = [
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr in {"author", "author_id"}
        ]
        self.assertEqual(offenders, [])

    def test_no_safe_filter_for_author_in_public_templates(self):
        import pathlib

        for path in (
            "templates/prompts/prompt_detail.html",
            "templates/prompts/prompt_list.html",
            "templates/partials/_editorial_card.html",
            "templates/partials/_seo_meta.html",
        ):
            text = pathlib.Path(path).read_text(encoding="utf-8")
            with self.subTest(path=path):
                for line in text.splitlines():
                    if "author" in line.lower():
                        self.assertNotIn("|safe", line)

    def test_no_public_write_access_to_live_author(self):
        import pathlib

        for path in ("prompts/views.py",):
            text = pathlib.Path(path).read_text(encoding="utf-8")
            with self.subTest(path=path):
                self.assertNotIn("live_author =", text)
                self.assertNotIn("live_author=", text)
