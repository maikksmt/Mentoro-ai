"""
Beta 8.14 release audit: every internal link a public page actually renders
must resolve to a reachable target - in both languages.

This is deliberately a small number of broad, page-level tests rather than
hundreds of per-card assertions: each test renders a real public page,
extracts the internal hrefs from the response HTML, and follows them. That
covers list cards, related-content cards, homepage "latest content" teasers,
the starter CTA, the navbar and the global footer in one pass, and it keeps
working when the markup changes, because nothing here asserts on markup
structure.

Deliberate exclusions (see NON_NAVIGATION_HREFS / _is_checkable): the
Klaro "Cookie-Settings" footer entry is an intentional href="#" JS control
(templates/base.html), and authenticated/POST-only endpoints are checked
for "not 404/500" rather than 200.
"""
import re

from django.test import TestCase
from django.utils import timezone

from catalog.models import Category, Tool
from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin
from guides.models import Guide
from prompts.models import Prompt
from usecases.models import UseCase

HREF_RE = re.compile(r'href="([^"]+)"')

# Endpoints that legitimately answer with a redirect (login-gated) or are
# POST-only; they must merely not be broken.
REDIRECT_OK_PREFIXES = (
    "/en/account/",
    "/de/account/",
    "/accounts/",
    "/i18n/",
)


def _publish(model, slug, languages, **extra):
    obj = model.objects.create(
        status=EditorialWorkflowMixin.STATUS_PUBLISHED,
        published_at=timezone.now(),
        **extra,
    )
    for lang in languages:
        obj.create_translation(
            lang, title=f"T {slug} {lang}", intro="intro", body="body", slug=f"{slug}-{lang}"
        )
    return obj


class PublicLinkIntegrityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        category = Category.objects.create()
        category.create_translation("en", name="Cat EN", slug="li-cat-en")
        category.create_translation("de", name="Cat DE", slug="li-cat-de")

        tool = Tool.objects.create(slug="li-tool", published_at=timezone.now())
        tool.create_translation("en", name="Tool EN", short_description="s")
        tool.create_translation("de", name="Tool DE", short_description="s")
        tool.categories.add(category)

        # Bilingual content of every editorial type, plus a starter guide and
        # single-language items that must never surface in the other language.
        starter = _publish(Guide, "li-starter", ("en", "de"), is_starter=True)
        starter.categories.add(category)
        starter.tools.add(tool)

        for kind, model in (
            ("g", Guide), ("p", Prompt), ("u", UseCase), ("c", Comparison),
        ):
            bi = _publish(model, f"li-bi-{kind}", ("en", "de"))
            if hasattr(bi, "tools"):
                bi.tools.add(tool)
            # single-language siblings: related/list/teaser queries must not
            # surface these under the other language prefix
            _publish(model, f"li-enonly-{kind}", ("en",))
            _publish(model, f"li-deonly-{kind}", ("de",))

    # -- helpers ---------------------------------------------------------

    def _internal_hrefs(self, html):
        hrefs = set()
        for raw in HREF_RE.findall(html):
            href = raw.split("#", 1)[0].strip()
            if not href or not href.startswith("/"):
                continue
            if href.startswith("/admin") or href.startswith("/static") or href.startswith("/media"):
                continue
            hrefs.add(href)
        return sorted(hrefs)

    def _assert_page_links_ok(self, page_path):
        resp = self.client.get(page_path)
        self.assertEqual(resp.status_code, 200, f"page itself failed: {page_path}")
        html = resp.content.decode()

        hrefs = self._internal_hrefs(html)
        self.assertTrue(hrefs, f"no internal links found on {page_path}")

        for href in hrefs:
            with self.subTest(page=page_path, href=href):
                status = self.client.get(href).status_code
                if href.startswith(REDIRECT_OK_PREFIXES):
                    self.assertNotIn(status, (404, 500), f"{href} broken (from {page_path})")
                else:
                    self.assertEqual(status, 200, f"{href} -> {status} (linked from {page_path})")
        return html

    def _assert_no_empty_or_hash_card_links(self, html, page_path):
        """A rendered teaser/card must never fall back to url='#'."""
        bad = [h for h in HREF_RE.findall(html) if h.strip() in ("", "#")]
        # The Klaro cookie-settings control is the single intentional href="#".
        self.assertLessEqual(
            len(bad), 1,
            f"{page_path} renders unexpected empty/# links: {bad}",
        )

    # -- list & home surfaces -------------------------------------------

    def test_homepage_links_are_reachable(self):
        for lang in ("en", "de"):
            with self.subTest(lang=lang):
                html = self._assert_page_links_ok(f"/{lang}/")
                self._assert_no_empty_or_hash_card_links(html, f"/{lang}/")

    def test_list_page_links_are_reachable(self):
        for lang in ("en", "de"):
            for path in (
                f"/{lang}/guides/",
                f"/{lang}/prompts/",
                f"/{lang}/usecases/",
                f"/{lang}/compare/",
                f"/{lang}/catalog/",
                f"/{lang}/glossary/",
            ):
                with self.subTest(path=path):
                    html = self._assert_page_links_ok(path)
                    self._assert_no_empty_or_hash_card_links(html, path)

    # -- detail surfaces (incl. related-content widgets) -----------------

    def test_detail_page_links_are_reachable(self):
        for lang in ("en", "de"):
            for path in (
                f"/{lang}/guides/li-bi-g-{lang}/",
                f"/{lang}/prompts/li-bi-p-{lang}/",
                f"/{lang}/usecases/li-bi-u-{lang}/",
                f"/{lang}/compare/li-bi-c-{lang}/",
            ):
                with self.subTest(path=path):
                    html = self._assert_page_links_ok(path)
                    self._assert_no_empty_or_hash_card_links(html, path)

    def test_detail_pages_never_link_to_the_other_languages_slugs(self):
        for lang in ("en", "de"):
            other = "de" if lang == "en" else "en"
            for kind in ("g", "p", "u", "c"):
                path = {
                    "g": f"/{lang}/guides/li-bi-g-{lang}/",
                    "p": f"/{lang}/prompts/li-bi-p-{lang}/",
                    "u": f"/{lang}/usecases/li-bi-u-{lang}/",
                    "c": f"/{lang}/compare/li-bi-c-{lang}/",
                }[kind]
                html = self.client.get(path).content.decode()
                with self.subTest(path=path, kind=kind):
                    # content that exists only in the other language must never
                    # be linked (or named) under this language's prefix
                    bad = re.findall(rf'href="[^"]*li-{other}only-[^"]*"', html)
                    self.assertEqual(
                        bad, [], f"{path} links to {other}-only content: {bad}"
                    )

    # -- scheduled tools (Beta 8.14a) ------------------------------------

    def test_no_public_page_links_to_a_tool_scheduled_for_the_future(self):
        """A tool with a future published_at must not be linked anywhere
        public - and every rendered link must still resolve (the broad
        crawl above would already fail on a 404, this pins the cause)."""
        future = Tool.objects.create(
            slug="li-future-tool",
            published_at=timezone.now() + timezone.timedelta(days=7),
            is_featured=True,
        )
        for lang in ("en", "de"):
            future.create_translation(lang, name=f"Future Tool {lang}", short_description="s")

        for lang in ("en", "de"):
            for path in (f"/{lang}/", f"/{lang}/catalog/"):
                with self.subTest(path=path):
                    html = self.client.get(path).content.decode()
                    self.assertNotIn("li-future-tool", html)
            with self.subTest(lang=lang, check="detail"):
                self.assertEqual(
                    self.client.get(f"/{lang}/catalog/li-future-tool/").status_code, 404
                )

    # -- starter CTA -----------------------------------------------------

    def test_starter_cta_target_is_reachable_in_both_languages(self):
        for lang in ("en", "de"):
            with self.subTest(lang=lang):
                html = self.client.get(f"/{lang}/").content.decode()
                targets = re.findall(rf'href="(/{lang}/guides/li-starter-{lang}/)"', html)
                self.assertTrue(targets, f"starter guide not linked on the {lang} homepage")
                self.assertEqual(self.client.get(targets[0]).status_code, 200)
