"""
Beta 11.11D4B: a published prompt never advertises a tool the public cannot
open.

The Beta 11.11D4A audit found that ``templates/prompts/prompt_detail.html``
iterated ``object.tools.all`` - the plain M2M, with no visibility filter at
all - while ``ToolDetailView`` has answered 404 for a non-public tool since
Beta 8.14a. A published prompt therefore rendered a full tool card (logo,
name, vendor, rating, "Details" button) linking straight into a 404.

The canonical public gate is ``Tool.objects.public()``
(``published_at <= now``, see ``catalog/models.py::ToolQuerySet``) - the same
single rule the catalogue list, the tool detail view, the sitemap, the search
adapters and ``compare/presentation.py`` already use. D4B reuses exactly that
query; it invents no second visibility contract and adds no tool status field.

Two properties of this codebase shape what these tests can assert:

* **Tools are not language-bound for reachability.** ``Tool.slug`` is a shared,
  untranslated field and ``PARLER_LANGUAGES`` sets ``hide_untranslated=False``,
  so an EN-only tool is deliberately reachable under ``/de/`` too (see
  ``catalog/tests/test_language_fallback.py``). D4B therefore adds no language
  logic; it asserts that EN and DE behave identically, which is the real
  contract.
* **Tool membership is not snapshotted.** Unlike Comparison's ``live_entries``,
  Prompt has no live tool snapshot - ``live_i18n`` holds translated content and
  ``live_author`` the author, nothing else. The M2M *is* the only membership
  source, and giving it a snapshot would mean new snapshot fields, which D4B is
  explicitly not allowed to add. So D4B changes only the *visibility filter*
  over that unchanged membership source.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from catalog.models import Tool
from prompts.models import Prompt

User = get_user_model()

PAST = -1
FUTURE = 7


def make_tool(slug, *, days=PAST, name=None):
    """A tool that is public (``days`` in the past) or not yet public."""
    tool = Tool.objects.create(
        slug=slug,
        published_at=timezone.now() + timezone.timedelta(days=days),
        vendor=f"Vendor {slug}",
    )
    for language in ("en", "de"):
        tool.create_translation(
            language,
            name=name or f"ToolName{slug}{language.upper()}",
            short_description="s",
        )
    return tool


def publish(prompt, by):
    """
    The real FSM publish, so ``live_i18n`` is written by the production code
    rather than hand-assembled, plus the live-revision marker the admin sets
    via ``core.admin.set_last_published_revision()`` - mirrors
    ``usecases/tests/live_visibility_fixtures.py::publish``.
    """
    prompt.move_to_review(by=by)
    prompt.save()
    prompt.approve(by=by)
    prompt.save()
    prompt.publish(by=by)
    prompt.save()

    fresh = Prompt.objects.get(pk=prompt.pk)
    fresh.last_published_revision_id = 1
    fresh.save(update_fields=["last_published_revision_id"])
    return fresh


def make_published_prompt(*, slug, title, tools=(), author=None, languages=None):
    """A prompt published through the real workflow, with its tools linked."""
    author = author or User.objects.create_user(f"d4b-author-{slug}", password="pw")
    prompt = Prompt.objects.create(author=author)
    for language, language_slug, language_title in (
        languages or (("en", slug, title),)
    ):
        prompt.create_translation(
            language, title=language_title, intro="i", body="b", outro="o",
            slug=language_slug,
        )
    for tool in tools:
        prompt.tools.add(tool)
    return publish(prompt, author)


class PublishedPromptToolRenderingTests(TestCase):
    def detail(self, prompt, language="en"):
        return self.client.get(f"/{language}/prompts/{prompt.public_slug or prompt.slug}/")

    def html(self, prompt, language="en"):
        resp = self.detail(prompt, language)
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    # -- 5.1 the public tool stays visible ------------------------------

    def test_a_public_tool_is_rendered_and_its_detail_page_is_reachable(self):
        tool = make_tool("d4b-public")
        prompt = make_published_prompt(slug="d4b-p-public", title="P Public", tools=[tool])

        html = self.html(prompt)
        self.assertIn("ToolNamed4b-publicEN", html)
        self.assertIn(reverse("catalog:detail", kwargs={"slug": tool.slug}), html)
        self.assertEqual(
            self.client.get(reverse("catalog:detail", kwargs={"slug": tool.slug})).status_code,
            200,
        )

    # -- 5.2 a tool that stopped being public disappears ----------------

    def test_a_tool_that_became_non_public_after_publish_is_omitted(self):
        """No republish of the prompt: the filter runs at render time."""
        tool = make_tool("d4b-later-hidden")
        prompt = make_published_prompt(slug="d4b-p-hidden", title="P Hidden", tools=[tool])
        self.assertIn("ToolNamed4b-later-hiddenEN", self.html(prompt))

        Tool.objects.filter(pk=tool.pk).update(
            published_at=timezone.now() + timezone.timedelta(days=FUTURE)
        )

        html = self.html(prompt)
        self.assertNotIn("ToolNamed4b-later-hiddenEN", html)
        self.assertNotIn(reverse("catalog:detail", kwargs={"slug": tool.slug}), html)
        self.assertNotIn("Vendor d4b-later-hidden", html)
        self.assertEqual(
            self.client.get(reverse("catalog:detail", kwargs={"slug": tool.slug})).status_code,
            404,
        )

    def test_the_tools_section_is_absent_entirely_when_no_tool_is_public(self):
        """Not an empty card and not an empty section heading."""
        tool = make_tool("d4b-only-hidden", days=FUTURE)
        prompt = make_published_prompt(slug="d4b-p-empty", title="P Empty", tools=[tool])

        html = self.html(prompt)
        self.assertNotIn("Suitable tools", html)
        self.assertNotIn("ToolNamed4b-only-hiddenEN", html)
        self.assertNotIn(reverse("catalog:detail", kwargs={"slug": tool.slug}), html)

    # -- 5.3 mixed list keeps its relative order ------------------------

    def test_a_mixed_list_renders_only_public_tools_in_their_original_order(self):
        first = make_tool("d4b-mix-1")
        hidden = make_tool("d4b-mix-2", days=FUTURE)
        last = make_tool("d4b-mix-3")
        prompt = make_published_prompt(
            slug="d4b-p-mixed", title="P Mixed", tools=[first, hidden, last]
        )

        html = self.html(prompt)
        self.assertNotIn("ToolNamed4b-mix-2EN", html)

        expected_order = [
            tool.pk
            for tool in Prompt.objects.get(pk=prompt.pk).tools.public()
        ]
        positions = [
            html.find(reverse("catalog:detail", kwargs={"slug": slug}))
            for slug in ("d4b-mix-1", "d4b-mix-3")
        ]
        self.assertNotIn(-1, positions, "a public tool link is missing")
        rendered_order = [
            pk for _pos, pk in sorted(
                zip(positions, [first.pk, last.pk], strict=True)
            )
        ]
        self.assertEqual(rendered_order, expected_order)
        self.assertEqual(html.count(reverse("catalog:detail", kwargs={"slug": "d4b-mix-1"})), 1)

    # -- 5.4 a tool that was never public -------------------------------

    def test_a_tool_that_was_never_public_is_omitted_from_the_start(self):
        never = make_tool("d4b-never", days=FUTURE)
        public = make_tool("d4b-alongside")
        prompt = make_published_prompt(
            slug="d4b-p-never", title="P Never", tools=[never, public]
        )

        html = self.html(prompt)
        self.assertNotIn("ToolNamed4b-neverEN", html)
        self.assertIn("ToolNamed4b-alongsideEN", html)

    # -- 5.5 a deleted tool -------------------------------------------

    def test_a_deleted_tool_leaves_the_page_reachable_and_link_free(self):
        """Prompt.tools is a plain M2M, so a hard delete removes the row from
        the join table - there is no orphan id to skip. Asserted so the
        behaviour is pinned rather than assumed."""
        doomed = make_tool("d4b-doomed")
        kept = make_tool("d4b-kept")
        prompt = make_published_prompt(
            slug="d4b-p-deleted", title="P Deleted", tools=[doomed, kept]
        )
        slug = doomed.slug
        doomed.delete()

        html = self.html(prompt)
        self.assertNotIn(reverse("catalog:detail", kwargs={"slug": slug}), html)
        self.assertIn("ToolNamed4b-keptEN", html)

    # -- 5.6 language: tools are not language-bound ---------------------

    def test_the_filter_behaves_identically_under_both_language_prefixes(self):
        """Tool reachability is language-independent by design (shared slug,
        ``hide_untranslated=False``), so D4B must not make EN and DE differ."""
        public = make_tool("d4b-lang-public")
        hidden = make_tool("d4b-lang-hidden", days=FUTURE)
        make_published_prompt(
            slug="d4b-p-lang-en",
            title="P Lang EN",
            tools=[public, hidden],
            languages=(
                ("en", "d4b-p-lang-en", "P Lang EN"),
                ("de", "d4b-p-lang-de", "P Lang DE"),
            ),
        )

        for language, slug in (("en", "d4b-p-lang-en"), ("de", "d4b-p-lang-de")):
            with self.subTest(language=language):
                resp = self.client.get(f"/{language}/prompts/{slug}/")
                self.assertEqual(resp.status_code, 200)
                html = resp.content.decode()
                self.assertIn(
                    reverse("catalog:detail", kwargs={"slug": public.slug}), html
                )
                self.assertNotIn(
                    reverse("catalog:detail", kwargs={"slug": hidden.slug}), html
                )


class PublicToolResolutionQueryTests(TestCase):
    """Beta 11.11D4B performance contract: resolving the tools of one prompt
    must stay a single query regardless of how many tools are linked."""

    @classmethod
    def setUpTestData(cls):
        cls.tools = [make_tool(f"d4b-q-{i}", days=PAST if i % 2 else FUTURE) for i in range(6)]
        cls.prompt = make_published_prompt(
            slug="d4b-p-query", title="P Query", tools=cls.tools
        )

    def test_resolving_the_public_tools_takes_exactly_one_query(self):
        prompt = Prompt.objects.get(pk=self.prompt.pk)
        with self.assertNumQueries(1):
            list(prompt.tools.public())

    def test_the_number_of_queries_does_not_grow_with_the_number_of_tools(self):
        small = make_published_prompt(
            slug="d4b-p-query-small", title="P Small", tools=self.tools[:1]
        )
        one_tool = Prompt.objects.get(pk=small.pk)
        six_tools = Prompt.objects.get(pk=self.prompt.pk)

        with self.assertNumQueries(1):
            list(one_tool.tools.public())
        with self.assertNumQueries(1):
            list(six_tools.tools.public())

    def test_only_public_tools_survive_the_resolution(self):
        prompt = Prompt.objects.get(pk=self.prompt.pk)
        resolved = list(prompt.tools.public())
        expected = [tool for tool in self.tools if tool.published_at <= timezone.now()]
        self.assertEqual({t.pk for t in resolved}, {t.pk for t in expected})
        self.assertEqual(len(resolved), 3)
