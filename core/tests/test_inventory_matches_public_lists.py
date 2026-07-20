"""
Beta 8.14 release audit: the homepage/footer inventory counts must agree
with what the public list pages actually serve - per language, for all four
editorial types.

core/tests/test_public_inventory.py already covers the counts against the
list *querysets* (and for Prompt specifically against
visible_in_language()); this closes the remaining gap by comparing the
counts against the paginator of the actually rendered list *responses*, so
a divergence between "what we count" and "what a visitor can page through"
would fail here even if both sides' querysets drifted together.
"""
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from compare.models import Comparison
from core.models.editorial import EditorialWorkflowMixin
from core.services import get_public_inventory
from guides.models import Guide
from prompts.models import Prompt
from usecases.models import UseCase

# (inventory key, model, list path suffix)
SURFACES = (
    ("guides", Guide, "guides"),
    ("prompts", Prompt, "prompts"),
    ("usecases", UseCase, "usecases"),
    ("comparisons", Comparison, "compare"),
)


def publish(model, slug, languages, **extra):
    obj = model.objects.create(
        status=EditorialWorkflowMixin.STATUS_PUBLISHED,
        published_at=timezone.now(),
        **extra,
    )
    for lang in languages:
        obj.create_translation(
            lang, title=f"T {slug} {lang}", intro="i", body="b", slug=f"{slug}-{lang}"
        )
    return obj


class InventoryMatchesRenderedListsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        for key, model, _path in SURFACES:
            publish(model, f"inv-{key}-en", ("en",))
            publish(model, f"inv-{key}-de", ("de",))
            publish(model, f"inv-{key}-bi", ("en", "de"))
            model.objects.create(status=EditorialWorkflowMixin.STATUS_DRAFT).create_translation(
                "en", title="d", intro="i", body="b", slug=f"inv-{key}-draft-en"
            )

    def _rendered_count(self, lang, path):
        resp = self.client.get(f"/{lang}/{path}/")
        self.assertEqual(resp.status_code, 200)
        paginator = resp.context.get("paginator")
        if paginator is not None:
            return paginator.count
        for key in ("object_list", "objects"):
            if resp.context.get(key) is not None:
                return len(resp.context[key])
        self.fail(f"could not determine rendered object count for /{lang}/{path}/")

    def test_inventory_counts_match_rendered_list_pages(self):
        for lang in ("en", "de"):
            counts = get_public_inventory(lang)["counts"]
            for key, _model, path in SURFACES:
                with self.subTest(lang=lang, kind=key):
                    self.assertEqual(
                        counts[key],
                        self._rendered_count(lang, path),
                        f"{key} inventory count disagrees with the rendered "
                        f"/{lang}/{path}/ list",
                    )

    def test_each_language_sees_exactly_its_own_two_items(self):
        """One single-language item + one bilingual item per language."""
        for lang in ("en", "de"):
            counts = get_public_inventory(lang)["counts"]
            for key, _model, _path in SURFACES:
                with self.subTest(lang=lang, kind=key):
                    self.assertEqual(counts[key], 2)

    def test_drafts_are_in_neither_the_counts_nor_the_lists(self):
        for lang in ("en", "de"):
            for key, _model, path in SURFACES:
                with self.subTest(lang=lang, kind=key):
                    html = self.client.get(f"/{lang}/{path}/").content.decode()
                    self.assertNotIn(f"inv-{key}-draft-en", html)
