"""
Beta 11.11D4B: a published use case never advertises a tool the public cannot
open.

Same defect and same fix as ``prompts/tests/test_public_tool_rendering.py``
(see that module for the full contract), with one addition specific to this
surface: ``UseCaseDetailView.get_context_data()`` also copies the linked tools
into the page's JSON-LD ``about`` block, with ``absolute_url(...)`` links. That
is a second public output of the same unfiltered M2M, so a non-public tool
leaked into structured data as well - asserted separately below.

The canonical gate stays ``Tool.objects.public()`` (``published_at <= now``);
D4B adds no tool status, no snapshot field and no language rule (tool
reachability is deliberately language-independent - shared slug,
``hide_untranslated=False``).
"""
import json

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from catalog.models import Tool
from usecases.models import UseCase
from usecases.tests.live_visibility_fixtures import make_user, publish

PAST = -1
FUTURE = 7


def make_tool(slug, *, days=PAST):
    tool = Tool.objects.create(
        slug=slug,
        published_at=timezone.now() + timezone.timedelta(days=days),
        vendor=f"Vendor {slug}",
    )
    for language in ("en", "de"):
        tool.create_translation(
            language, name=f"ToolName{slug}{language.upper()}", short_description="s"
        )
    return tool


def make_published_usecase(*, slug, title, tools=(), languages=(("en", None),)):
    """Published through the shared Beta 11.7 fixture helper, so ``live_i18n``
    comes from the real FSM publish rather than a hand-built dict."""
    author = make_user(f"d4b-uc-author-{slug}")
    usecase = UseCase.objects.create(author=author)
    for language, language_slug in languages:
        usecase.create_translation(
            language, title=title, intro="i", body="b", outro="o",
            slug=language_slug or slug, persona="P",
        )
    for tool in tools:
        usecase.tools.add(tool)
    return publish(usecase, author)


class PublishedUseCaseToolRenderingTests(TestCase):
    def html(self, slug, language="en"):
        resp = self.client.get(f"/{language}/usecases/{slug}/")
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    # -- 5.1 the public tool stays visible ------------------------------

    def test_a_public_tool_is_rendered_and_its_detail_page_is_reachable(self):
        tool = make_tool("d4b-uc-public")
        make_published_usecase(slug="d4b-uc-p", title="UC Public", tools=[tool])

        html = self.html("d4b-uc-p")
        self.assertIn("ToolNamed4b-uc-publicEN", html)
        self.assertIn(reverse("catalog:detail", kwargs={"slug": tool.slug}), html)
        self.assertEqual(
            self.client.get(reverse("catalog:detail", kwargs={"slug": tool.slug})).status_code,
            200,
        )

    # -- 5.2 a tool that stopped being public disappears ----------------

    def test_a_tool_that_became_non_public_after_publish_is_omitted(self):
        tool = make_tool("d4b-uc-hidden")
        make_published_usecase(slug="d4b-uc-h", title="UC Hidden", tools=[tool])
        self.assertIn("ToolNamed4b-uc-hiddenEN", self.html("d4b-uc-h"))

        Tool.objects.filter(pk=tool.pk).update(
            published_at=timezone.now() + timezone.timedelta(days=FUTURE)
        )

        html = self.html("d4b-uc-h")
        self.assertNotIn("ToolNamed4b-uc-hiddenEN", html)
        self.assertNotIn(reverse("catalog:detail", kwargs={"slug": tool.slug}), html)
        self.assertNotIn("Vendor d4b-uc-hidden", html)
        self.assertEqual(
            self.client.get(reverse("catalog:detail", kwargs={"slug": tool.slug})).status_code,
            404,
        )

    def test_the_tools_section_is_absent_entirely_when_no_tool_is_public(self):
        tool = make_tool("d4b-uc-only-hidden", days=FUTURE)
        make_published_usecase(slug="d4b-uc-e", title="UC Empty", tools=[tool])

        html = self.html("d4b-uc-e")
        self.assertNotIn("Tools used", html)
        self.assertNotIn("ToolNamed4b-uc-only-hiddenEN", html)

    # -- the JSON-LD surface of the very same list ----------------------

    def test_a_non_public_tool_never_reaches_the_structured_data(self):
        public = make_tool("d4b-uc-ld-public")
        hidden = make_tool("d4b-uc-ld-hidden", days=FUTURE)
        make_published_usecase(
            slug="d4b-uc-ld", title="UC LD", tools=[public, hidden]
        )

        html = self.html("d4b-uc-ld")
        self.assertNotIn("ToolNamed4b-uc-ld-hiddenEN", html)

        blocks = [
            json.loads(chunk.split("</script>", 1)[0])
            for chunk in html.split('type="application/ld+json">')[1:]
        ]
        about_names = [
            entry.get("name")
            for block in blocks
            for entry in (block.get("about") or [])
        ]
        self.assertIn("ToolNamed4b-uc-ld-publicEN", about_names)
        self.assertNotIn("ToolNamed4b-uc-ld-hiddenEN", about_names)

    # -- 5.3 mixed list keeps its relative order ------------------------

    def test_a_mixed_list_renders_only_public_tools_in_their_original_order(self):
        first = make_tool("d4b-uc-mix-1")
        hidden = make_tool("d4b-uc-mix-2", days=FUTURE)
        last = make_tool("d4b-uc-mix-3")
        usecase = make_published_usecase(
            slug="d4b-uc-mixed", title="UC Mixed", tools=[first, hidden, last]
        )

        html = self.html("d4b-uc-mixed")
        self.assertNotIn("ToolNamed4b-uc-mix-2EN", html)

        expected_order = [tool.pk for tool in usecase.tools.public()]
        positions = [
            html.find(reverse("catalog:detail", kwargs={"slug": slug}))
            for slug in ("d4b-uc-mix-1", "d4b-uc-mix-3")
        ]
        self.assertNotIn(-1, positions, "a public tool link is missing")
        rendered_order = [
            pk for _pos, pk in sorted(zip(positions, [first.pk, last.pk], strict=True))
        ]
        self.assertEqual(rendered_order, expected_order)

    # -- 5.4 / 5.5 never-public and deleted tools -----------------------

    def test_a_tool_that_was_never_public_is_omitted_from_the_start(self):
        never = make_tool("d4b-uc-never", days=FUTURE)
        public = make_tool("d4b-uc-alongside")
        make_published_usecase(
            slug="d4b-uc-n", title="UC Never", tools=[never, public]
        )

        html = self.html("d4b-uc-n")
        self.assertNotIn("ToolNamed4b-uc-neverEN", html)
        self.assertIn("ToolNamed4b-uc-alongsideEN", html)

    def test_a_deleted_tool_leaves_the_page_reachable_and_link_free(self):
        doomed = make_tool("d4b-uc-doomed")
        kept = make_tool("d4b-uc-kept")
        make_published_usecase(
            slug="d4b-uc-d", title="UC Deleted", tools=[doomed, kept]
        )
        slug = doomed.slug
        doomed.delete()

        html = self.html("d4b-uc-d")
        self.assertNotIn(reverse("catalog:detail", kwargs={"slug": slug}), html)
        self.assertIn("ToolNamed4b-uc-keptEN", html)

    # -- 5.6 language ---------------------------------------------------

    def test_the_filter_behaves_identically_under_both_language_prefixes(self):
        public = make_tool("d4b-uc-lang-public")
        hidden = make_tool("d4b-uc-lang-hidden", days=FUTURE)
        make_published_usecase(
            slug="d4b-uc-lang-en",
            title="UC Lang",
            tools=[public, hidden],
            languages=(("en", "d4b-uc-lang-en"), ("de", "d4b-uc-lang-de")),
        )

        for language, slug in (("en", "d4b-uc-lang-en"), ("de", "d4b-uc-lang-de")):
            with self.subTest(language=language):
                html = self.html(slug, language)
                self.assertIn(
                    reverse("catalog:detail", kwargs={"slug": public.slug}), html
                )
                self.assertNotIn(
                    reverse("catalog:detail", kwargs={"slug": hidden.slug}), html
                )


class UseCasePublicToolQueryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tools = [
            make_tool(f"d4b-ucq-{i}", days=PAST if i % 2 else FUTURE) for i in range(6)
        ]
        cls.usecase = make_published_usecase(
            slug="d4b-uc-query", title="UC Query", tools=cls.tools
        )

    def test_resolving_the_public_tools_takes_exactly_one_query(self):
        usecase = UseCase.objects.get(pk=self.usecase.pk)
        with self.assertNumQueries(1):
            list(usecase.tools.public())

    def test_only_public_tools_survive_the_resolution(self):
        usecase = UseCase.objects.get(pk=self.usecase.pk)
        self.assertEqual(len(list(usecase.tools.public())), 3)
