"""
Beta 8.11 Section B: reproduction of the same slug-priority bug confirmed
for Prompt/Guide, applied to UseCase.

Architectural note (documented, not changed in this slice): unlike Guide/
Prompt, UseCaseQuerySet.visible_in_language() uses the strict .published()
status rule, not the broader visible_on_site() - a use case that leaves
STATUS_PUBLISHED (e.g. via move_to_review()) disappears from every public
surface entirely, old slug included, so the classic "live revision in
progress" scenario used for Guide/Prompt cannot leak a draft slug here: it
404s on BOTH slugs instead. This is pre-existing, confirmed status
semantics ("keine unbeabsichtigte Erweiterung von published() auf
visible_on_site()") and is deliberately left unchanged.

What *is* realistic and worth guarding here: usecases/views.py::_resolve_by_slug()
had the identical structural bug as Prompt's - it checked the CURRENT
translation's slug/public_slug first, only falling back to live_i18n second.
The FSM-driven admin flow (EditorialWorkflowAdminMixin._must_auto_review())
prevents a *form-based* edit from ever leaving a status=PUBLISHED use case
with a translation slug that differs from its own live_i18n snapshot - but
nothing at the model/DB level enforces that invariant (e.g. a direct
`.update()`, a data migration, or a bulk import could still diverge them).
The test below reproduces that divergence directly against a still-
`published()` (i.e. still realistically, currently publicly visible) use
case, which is exactly the scenario the shared live-snapshot-first
resolver order is supposed to guard against for every editorial model.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from core.models.editorial import EditorialWorkflowMixin
from usecases.models import UseCase

User = get_user_model()


class UseCaseStatusSemanticsDuringRevisionTests(TestCase):
    """Documents the real (unchanged) status semantics: a use case mid-
    revision (status=review) is fully invisible - old slug included."""

    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user(
            username="usecase-status-editor", email="usecase-status@example.com", password="testpass123"
        )

    def test_old_live_slug_404s_once_status_leaves_published(self):
        u = UseCase.objects.create(status=EditorialWorkflowMixin.STATUS_APPROVED, author=self.author)
        u.create_translation("en", title="Old Public Title", intro="i", body="b", outro="o",
                              slug="uc-old-live-slug-811", persona="Founder")
        u.publish(by=self.author)
        u.save()
        old_slug = u.public_slug or u.slug

        # Real workflow: start a new revision.
        u.slug = "uc-new-draft-slug-811"
        u.save()
        u.move_to_review(by=self.author)
        u.last_published_revision_id = 1
        u.save()

        # Both the old AND the new slug 404 - the object is not in
        # .published() at all while status=review, regardless of slug.
        resp_old = self.client.get(f"/en/usecases/{old_slug}/")
        self.assertEqual(resp_old.status_code, 404)
        resp_new = self.client.get("/en/usecases/uc-new-draft-slug-811/")
        self.assertEqual(resp_new.status_code, 404)


class UseCaseDraftSlugLeakReproductionTests(TestCase):
    """Realistic status where a live version stays public: status stays
    STATUS_PUBLISHED (matching .published()/visible_in_language()), but the
    translation's slug is made to diverge from the live_i18n snapshot
    directly (bypassing the admin's auto-review guard, exactly as a direct
    DB write / bulk import / data migration would)."""

    def test_diverged_slug_does_not_resolve_while_live_slug_does(self):
        u = UseCase.objects.create(status=EditorialWorkflowMixin.STATUS_PUBLISHED,
                                    published_at=timezone.now())
        u.create_translation("en", title="Live Title", intro="Live intro", body="Live body",
                              outro="Live outro", slug="uc-live-slug-811", persona="Founder")
        u.live_i18n = {
            "en": {
                "slug": "uc-live-slug-811",
                "public_slug": None,
                "title": "Live Title",
                "intro": "Live intro",
                "body": "Live body",
                "outro": "Live outro",
            }
        }
        u.last_published_revision_id = 1
        u.save()

        # Simulate a diverged current translation slug while status stays
        # PUBLISHED (not reachable via the guarded admin form, but not
        # prevented at the DB/model level either).
        u.slug = "uc-diverged-slug-811"
        u.save()

        resp_live = self.client.get("/en/usecases/uc-live-slug-811/")
        self.assertEqual(resp_live.status_code, 200)
        self.assertIn("Live Title", resp_live.content.decode())

        resp_diverged = self.client.get("/en/usecases/uc-diverged-slug-811/")
        self.assertEqual(resp_diverged.status_code, 404)

    def test_historical_published_usecase_without_live_snapshot_still_resolves(self):
        u = UseCase.objects.create(status=EditorialWorkflowMixin.STATUS_PUBLISHED,
                                    published_at=timezone.now())
        u.create_translation("en", title="Historical Title", intro="i", body="b", outro="o",
                              slug="uc-historical-slug-811", persona="Founder")
        self.assertEqual(u.live_i18n, {})

        resp = self.client.get("/en/usecases/uc-historical-slug-811/")
        self.assertEqual(resp.status_code, 200)
