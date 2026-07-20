"""
Beta 8.11 Section C: reproduction of two confirmed Comparison URL bugs.

1. Comparison.get_absolute_url(language=...) fallback-slug leak: an
   EN-only Comparison's get_absolute_url(language="de") used
   `switch_language(self, "de")` followed by plain attribute access
   (`self.slug`). PARLER_LANGUAGES has `fallback: "en"` and
   `hide_untranslated: False` (mentoroai/settings/base.py), so that raw
   attribute access silently substitutes the EN translation's slug instead
   of raising or returning nothing - the method never checked
   has_translation(language) first. Combined with i18n_next's unguarded
   exception-fallback call to obj.get_absolute_url(language=language_code)
   (core/templatetags/i18n_next.py), this produced a language switcher
   target combining the EN slug with the requested language's own prefix
   (e.g. "/de/compare/<en-slug>/"), which then 404s.

2. ComparisonDetailView.get_object() resolved strictly against the CURRENT
   translation's slug/public_slug field with no live_i18n check at all
   (unlike Guide's _resolve_guide_by_slug()/Prompt's/UseCase's
   _resolve_by_slug()) - so, structurally, a translation slug that diverges
   from the live_i18n snapshot while status stays PUBLISHED would resolve
   the diverged (non-live) slug and NOT the live one, the opposite of the
   Prompt/UseCase bug but the same root cause: no live-snapshot-first
   priority.

Required behavior after the fix:
    - get_absolute_url(language=<missing-translation>) never returns a URL
      combining another language's slug with that language's prefix (either
      "#" or nothing resolvable, matching Guide/Prompt's existing "#"
      convention for missing translations).
    - The detail resolver treats the live_i18n snapshot (when present) as
      the sole public slug for that language.
"""
from django.test import TestCase
from django.utils import timezone, translation

from compare.models import Comparison


def make_comparison(*, slug, languages=("en", "de")):
    c = Comparison.objects.create(status="published", published_at=timezone.now())
    for lang in languages:
        c.create_translation(lang, title=f"Title {slug} {lang}", intro="i", body="b", slug=f"{slug}-{lang}")
    return c


class ComparisonGetAbsoluteUrlLanguageSafetyTests(TestCase):
    def test_en_only_comparison_get_absolute_url_de_does_not_use_en_slug(self):
        c = make_comparison(slug="abs-url-en-only", languages=("en",))
        with translation.override("en"):
            url = c.get_absolute_url(language="de")
        self.assertNotIn("abs-url-en-only-en", url)

    def test_en_only_comparison_get_absolute_url_de_is_not_a_404_target(self):
        c = make_comparison(slug="abs-url-en-only-404check", languages=("en",))
        with translation.override("en"):
            url = c.get_absolute_url(language="de")
        if url and url != "#":
            resp = self.client.get(url)
            self.assertNotEqual(resp.status_code, 404, f"{url} must not be a broken switcher target")

    def test_de_only_comparison_get_absolute_url_en_does_not_use_de_slug(self):
        c = make_comparison(slug="abs-url-de-only", languages=("de",))
        with translation.override("de"):
            url = c.get_absolute_url(language="en")
        self.assertNotIn("abs-url-de-only-de", url)

    def test_bilingual_comparison_get_absolute_url_uses_correct_slug_per_language(self):
        c = make_comparison(slug="abs-url-bilingual", languages=("en", "de"))
        url_en = c.get_absolute_url(language="en")
        url_de = c.get_absolute_url(language="de")
        self.assertIn("abs-url-bilingual-en", url_en)
        self.assertIn("abs-url-bilingual-de", url_de)
        self.assertIn("/en/", url_en)
        self.assertIn("/de/", url_de)

    def test_language_switcher_target_for_en_only_comparison_is_not_broken(self):
        """End-to-end: rendering the EN detail page and following its
        i18n_next-generated DE switcher target must not land on a 404."""
        make_comparison(slug="switcher-en-only", languages=("en",))
        resp = self.client.get("/en/compare/switcher-en-only-en/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertNotIn('data-next="/de/compare/switcher-en-only-en/"', html)


class ComparisonDetailResolverLiveSnapshotPriorityTests(TestCase):
    def test_diverged_slug_does_not_resolve_while_live_slug_does(self):
        c = Comparison.objects.create(status="published", published_at=timezone.now())
        c.create_translation("en", title="Live Title", intro="Live intro", body="Live body",
                              slug="cmp-live-slug-811")
        c.live_i18n = {
            "en": {
                "slug": "cmp-live-slug-811",
                "public_slug": None,
                "title": "Live Title",
                "intro": "Live intro",
                "body": "Live body",
            }
        }
        c.last_published_revision_id = 1
        c.save()

        c.slug = "cmp-diverged-slug-811"
        c.save()

        resp_live = self.client.get("/en/compare/cmp-live-slug-811/")
        self.assertEqual(resp_live.status_code, 200)
        self.assertIn("Live Title", resp_live.content.decode())

        resp_diverged = self.client.get("/en/compare/cmp-diverged-slug-811/")
        self.assertEqual(resp_diverged.status_code, 404)

    def test_historical_published_comparison_without_live_snapshot_still_resolves(self):
        c = Comparison.objects.create(status="published", published_at=timezone.now())
        c.create_translation("en", title="Historical Title", intro="i", body="b",
                              slug="cmp-historical-slug-811")
        self.assertEqual(c.live_i18n, {})

        resp = self.client.get("/en/compare/cmp-historical-slug-811/")
        self.assertEqual(resp.status_code, 200)
