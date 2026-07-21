from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import get_language
from django_fsm import transition, FSMField
from parler.managers import TranslatableManager, TranslatableQuerySet
from parler.utils.context import switch_language


def get_snapshot_field(
    snapshot: dict | None,
    *,
    language_code: str,
    field_name: str,
    default: str = "",
):
    """
    Pure snapshot-field resolver for the published ``live_i18n`` contract
    shared by every :class:`EditorialWorkflowMixin` subclass and
    ``GuideSection`` - no DB queries, no Parler language activation, no
    cross-language fallback (mirrors the three-state contract already
    documented in ``core/projections.py``'s ``public_content_value()``,
    which reads the snapshot directly for exactly the same reason).

    Returns ``(is_published, value)``:

    - ``snapshot`` is falsy (``None`` or ``{}``): legacy content that was
      never run through the publish/snapshot mechanism -
      ``(False, default)``. Callers may fall back to the current
      same-language draft value in this case only.
    - ``snapshot`` is non-empty: it is authoritative regardless of whether
      ``language_code``/``field_name`` exist within it -
      ``(True, value)``. A missing language, a missing field, or an
      explicit ``None`` value all resolve to ``default``; an existing
      empty string is returned as-is. Never looks at any other language.
    """
    if not snapshot:
        return False, default
    language_data = snapshot.get(language_code)
    if not isinstance(language_data, dict):
        return True, default
    value = language_data.get(field_name, default)
    return True, (default if value is None else value)


# -------- Manager --------

class EditorialQuerySet(TranslatableQuerySet):
    """
    Parler-aware queryset with convenience filters for every workflow state (drafts, in_review, rework, published)
    and a visible_on_site scope that exposes published plus review only if a live revision exists.
    """

    def drafts(self):
        return self.filter(status=EditorialWorkflowMixin.STATUS_DRAFT).order_by("pk")

    def in_review(self):
        return self.filter(status=EditorialWorkflowMixin.STATUS_REVIEW).order_by("pk")

    def rework(self):
        return self.filter(status=EditorialWorkflowMixin.STATUS_REWORK).order_by("pk")

    def approved(self):
        return self.filter(status=EditorialWorkflowMixin.STATUS_APPROVED).order_by("pk")

    def published(self):
        return self.filter(status=EditorialWorkflowMixin.STATUS_PUBLISHED).order_by("published_at")

    def visible_on_site(self):
        """
        Public visibility filter:
        includes published items and review/approved items that already have a
        last_published_at/live revision, preventing premature exposure.
        """
        return (
            self.filter(
                Q(status=EditorialWorkflowMixin.STATUS_PUBLISHED)
                | Q(
                    status__in=[
                        EditorialWorkflowMixin.STATUS_REVIEW,
                        EditorialWorkflowMixin.STATUS_APPROVED,
                    ],
                    last_published_revision_id__isnull=False,
                )
            ).order_by("updated_at")
        )


class EditorialManager(TranslatableManager.from_queryset(EditorialQuerySet)):  # type: ignore
    """
    Translatable manager that exposes EditorialQuerySet; centralizes editorial filters for all content models.
    """
    pass


class PublishedOnlyManager(EditorialManager):
    """
    Manager that restricts queries to published items and to the active language;
    ideal for public pages where drafts must never leak.
    """

    def get_queryset(self):
        qs = super().get_queryset().published()
        # Parler: only active translations for actual language
        lang = get_language()
        qs = qs.active_translations(lang)
        return qs


# -------- Editorial Workflow Mixin --------


class EditorialWorkflowMixin(models.Model):
    """
    Abstract base implementing the finite-state machine (via django-fsm-2)
    for draft → review → (rework|published) plus archival;
    encapsulates transitions and side effects like snapshot updates.
    """
    STATUS_DRAFT = "draft"
    STATUS_REVIEW = "review"
    STATUS_REWORK = "rework"
    STATUS_APPROVED = "approved"
    STATUS_PUBLISHED = "published"
    STATUS_ARCHIVED = "archived"

    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_REVIEW, "Review"),
        (STATUS_REWORK, "Rework"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_PUBLISHED, "Published"),
        (STATUS_ARCHIVED, "Archived"),
    ]
    LIVE_SNAPSHOT_FIELDS = ("slug", "public_slug", "title")
    status = FSMField(default=STATUS_DRAFT, choices=STATUS_CHOICES, protected=True)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="%(class)s_author",
        on_delete=models.SET_NULL, null=True, blank=True
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)
    is_published = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True, blank=True)
    objects = EditorialManager()
    published = PublishedOnlyManager()
    submitted_for_review_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="%(class)s_reviewer"
    )
    review_note = models.TextField(blank=True)
    last_published_revision_id = models.IntegerField(null=True, blank=True)

    class Meta:
        abstract = True

    def _update_live_snapshot(self) -> None:
        if not hasattr(self, "live_i18n"):
            return

        if not hasattr(self, "get_available_languages"):
            return

        live = {}
        for lang in self.get_available_languages():
            with switch_language(self, lang):
                entry = {}
                for fname in getattr(self, "LIVE_SNAPSHOT_FIELDS", ()):
                    if hasattr(self, "safe_translation_getter"):
                        entry[fname] = self.safe_translation_getter(fname)
                    else:
                        entry[fname] = None
                live[lang] = entry

        self.live_i18n = live
        try:
            self.save(update_fields=["live_i18n"])
        except Exception:
            pass

    def get_live_value(self, field: str, language: str | None = None) -> str | None:
        """
        Returns the authoritative published-snapshot value for `field` in
        `language` - never falls back to another language. Returns None
        only when live_i18n itself is completely empty/missing, signaling
        callers to fall back to the current same-language draft value
        instead (see get_snapshot_field()).
        """
        lang = language or get_language()
        is_published, value = get_snapshot_field(
            self.live_i18n, language_code=lang, field_name=field,
        )
        return value if is_published else None

    # --- Transitions ---

    @transition(field=status, source=[STATUS_DRAFT, STATUS_REWORK, STATUS_PUBLISHED, STATUS_APPROVED], target=STATUS_REVIEW)
    def move_to_review(self, *, by, note: str | None = None):
        self.submitted_for_review_at = timezone.now()
        if note:
            try:
                self.review_note = note
            except Exception:
                pass

    @transition(field=status, source=STATUS_REVIEW, target=STATUS_REWORK)
    def request_rework(self, *, by, note=""):
        self.reviewed_at = timezone.now()
        self.reviewed_by = by
        self.review_note = note

    @transition(field=status, source=STATUS_REVIEW, target=STATUS_APPROVED)
    def approve(self, *, by, note=""):
        self.reviewed_at = timezone.now()
        self.reviewed_by = by
        if note:
            self.review_note = note

    @transition(field=status, source=STATUS_APPROVED, target=STATUS_PUBLISHED)
    def publish(self, *, by, note=""):
        now = timezone.now()
        if not self.is_published and (not self.published_at or self.published_at <= now):
            self.published_at = now

        self.updated_at = now
        if note:
            self.review_note = note
        self._update_live_snapshot()
        try:
            self.on_after_publish()
        except Exception:
            pass

    def on_after_publish(self) -> None:
        """
        Post-publish extension point for subclasses (e.g., cache busting, search indexing).
        """
        pass

    @transition(field=status, source="*", target=STATUS_ARCHIVED)
    def archive(self, *, by, note=""):
        """
        Soft-delete: marks content as archived without destroying history;
        excluded from public queries.
        """
        if note:
            self.review_note = note

    @transition(field=status, source=STATUS_ARCHIVED, target=STATUS_DRAFT)
    def restore(self, *, by, note=""):
        """
        Reverses archive to make content available to the workflow again.
        """
        if note:
            self.review_note = note
