from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models.editorial import EditorialWorkflowMixin
from guides.models import Guide


def create_guide(*, slug, en_title, de_title=None,
                 status=EditorialWorkflowMixin.STATUS_PUBLISHED, published_at=None,
                 is_starter=False, languages=("en", "de")):
    if published_at is None and status == EditorialWorkflowMixin.STATUS_PUBLISHED:
        published_at = timezone.now()
    g = Guide.objects.create(status=status, published_at=published_at, is_starter=is_starter)
    if "en" in languages:
        g.create_translation("en", slug=f"{slug}-en", title=en_title, intro="i", body="b")
    if "de" in languages:
        g.create_translation("de", slug=f"{slug}-de", title=de_title or en_title, intro="i", body="b")
    return g


class GuideListViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.pub = create_guide(slug="visible", en_title="Visible")
        cls.pub2 = create_guide(
            slug="another", en_title="Another",
            published_at=timezone.now() - timezone.timedelta(minutes=1)
        )
        cls.draft = create_guide(slug="hidden-draft", en_title="Hidden Draft",
                                 status=EditorialWorkflowMixin.STATUS_DRAFT)

    def _list(self):
        url = reverse("guides:list")
        return self.client.get(url)

    def _detail(self, slug):
        url = reverse("guides:detail", kwargs={"slug": slug})
        return self.client.get(url)

    def test_list_only_published_and_ordering(self):
        # Beta 8.6: listing order is driven by updated_at (recency of the
        # actual edit), not published_at, so pin both explicitly here.
        now = timezone.now()
        Guide.objects.filter(pk=self.pub.pk).update(updated_at=now - timezone.timedelta(minutes=5))
        Guide.objects.filter(pk=self.pub2.pk).update(updated_at=now)

        resp = self._list()
        self.assertEqual(resp.status_code, 200)
        objs = list(resp.context["object_list"])
        self.assertTrue(all(o.status == EditorialWorkflowMixin.STATUS_PUBLISHED for o in objs))
        self.assertEqual([o.pk for o in objs], [self.pub2.pk, self.pub.pk])

    def test_pagination_is_stable(self):
        for i in range(22):
            create_guide(slug=f"more-{i}", en_title=f"More {i}",
                         published_at=timezone.now() - timezone.timedelta(minutes=i))
        resp = self._list()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("paginator", resp.context)
        self.assertEqual(resp.context["paginator"].per_page, 15)

    def test_detail_i18n_and_canonical(self):
        resp = self._detail(self.pub.slug)
        html = resp.content.decode("utf-8")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('<link rel="canonical"', html)
        self.assertIn(self.pub.slug, html)
        if 'rel="alternate"' in html:
            self.assertIn('hreflang="en"', html)
            self.assertIn('hreflang="de"', html)


class GuideListStarterOrderingTests(TestCase):
    def _list(self):
        return self.client.get(reverse("guides:list"))

    def test_starter_appears_first_regardless_of_its_own_updated_at(self):
        now = timezone.now()
        starter = create_guide(slug="the-starter", en_title="Starter", is_starter=True)
        recent = create_guide(slug="recent", en_title="Recent")
        Guide.objects.filter(pk=starter.pk).update(updated_at=now - timezone.timedelta(days=30))
        Guide.objects.filter(pk=recent.pk).update(updated_at=now)

        objs = list(self._list().context["object_list"])
        self.assertEqual(objs[0].pk, starter.pk)
        self.assertEqual([o.pk for o in objs], [starter.pk, recent.pk])

    def test_starter_appears_exactly_once(self):
        starter = create_guide(slug="unique-starter", en_title="Starter", is_starter=True)
        objs = list(self._list().context["object_list"])
        self.assertEqual([o.pk for o in objs].count(starter.pk), 1)

    def test_start_here_badge_only_on_starter(self):
        create_guide(slug="badge-starter", en_title="Starter", is_starter=True)
        create_guide(slug="badge-plain", en_title="Plain")
        html = self._list().content.decode()
        self.assertEqual(html.count("Start here"), 1)

    def test_remaining_guides_ordered_by_updated_at_then_published_at(self):
        now = timezone.now()
        a = create_guide(slug="a", en_title="A", published_at=now - timezone.timedelta(days=1))
        b = create_guide(slug="b", en_title="B", published_at=now - timezone.timedelta(days=2))
        Guide.objects.filter(pk=a.pk).update(updated_at=now - timezone.timedelta(days=1))
        Guide.objects.filter(pk=b.pk).update(updated_at=now - timezone.timedelta(days=1))

        objs = list(self._list().context["object_list"])
        # Tied updated_at -> published_at decides: b (older publish) sorts
        # after a? No: -published_at means most-recently-published first, so
        # a (published 1 day ago) must precede b (published 2 days ago).
        self.assertEqual([o.pk for o in objs], [a.pk, b.pk])

    def test_stable_pk_tiebreaker(self):
        now = timezone.now()
        first = create_guide(slug="tie-1", en_title="Tie1", published_at=now)
        second = create_guide(slug="tie-2", en_title="Tie2", published_at=now)
        Guide.objects.filter(pk__in=[first.pk, second.pk]).update(updated_at=now)

        objs = list(self._list().context["object_list"])
        self.assertEqual([o.pk for o in objs], [second.pk, first.pk])

    def test_unpublished_guides_absent(self):
        create_guide(slug="draft-only", en_title="Draft",
                      status=EditorialWorkflowMixin.STATUS_DRAFT, published_at=None)
        objs = list(self._list().context["object_list"])
        self.assertEqual(objs, [])

    def test_guide_without_current_language_translation_absent(self):
        create_guide(slug="de-only", en_title="German only", languages=("de",))
        objs = list(self._list().context["object_list"])
        self.assertEqual(objs, [])

    def test_legacy_slug_alone_does_not_make_a_guide_the_starter(self):
        create_guide(slug="start-guide", en_title="Legacy slug", is_starter=False)
        html = self._list().content.decode()
        self.assertNotIn("Start here", html)

    def test_is_starter_works_with_any_slug(self):
        create_guide(slug="arbitrary-name", en_title="Arbitrary", is_starter=True)
        html = self._list().content.decode()
        self.assertIn("Start here", html)
