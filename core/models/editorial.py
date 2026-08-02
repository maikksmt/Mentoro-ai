import logging

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import get_language
from django_fsm import FSMField, transition
from parler.managers import TranslatableManager, TranslatableQuerySet
from parler.utils.context import switch_language

logger = logging.getLogger(__name__)


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

    #: Editing states in which content that was already published keeps its
    #: public presence, provided the publication itself is still provable.
    #:
    #: STATUS_REWORK joined this set in Beta 11.11B2A. Use cases already had it
    #: (Beta 11.7A) and comparisons too (Beta 11.9), each via a local override
    #: whose reasoning was never guide/prompt-specific: "rework" means the *new*
    #: draft needs another pass, not that the previously published snapshot was
    #: withdrawn, so taking the page offline for the duration of an editorial
    #: round is a defect rather than a safety measure. Guide and Prompt were
    #: simply never migrated to that conclusion.
    #:
    #: STATUS_DRAFT joined in Beta 11.11D1. Until then it was excluded on the
    #: grounds that "the FSM only reaches draft from archived via restore(),
    #: i.e. after a withdrawal" - true at the time, but no longer: D1 makes
    #: every automatic review/approval invalidation target ``draft`` (see
    #: ``core.review_binding.target_status_after_review_invalidation``), so
    #: draft is now the ordinary state of a published page whose next version
    #: is being written. Excluding it would take exactly the pages offline
    #: that B2A's rework detour existed to keep online. The archived ->
    #: restore() path stays excluded on a different, stronger ground: the
    #: publication proof below, since ``archive()`` clears ``is_published``.
    #:
    #: STATUS_ARCHIVED stays out - archiving *is* the deliberate public
    #: withdrawal and outranks any snapshot still on record.
    #:
    #: Spelled as literals rather than ``EditorialWorkflowMixin.STATUS_*``
    #: because that class is defined further down this module and a class-body
    #: reference would raise NameError at import time. The two stay in lockstep
    #: through an explicit contract test rather than through an import cycle.
    LIVE_EDITING_STATUSES = ("draft", "review", "approved", "rework")

    def live_snapshot_language_q(self, language_code: str) -> Q:
        """
        Beta 11.11D1: the language half of the public contract, shared by
        every editorial type so the three surfaces that used to spell it out
        separately cannot drift apart.

        Mirrors ``core/projections.py``'s three snapshot states exactly:

        * ``live_i18n`` has an entry for ``language_code`` - published here;
        * ``live_i18n`` is entirely empty/NULL - a record predating the
          snapshot mechanism, kept on its pre-snapshot behaviour (only ever
          reachable through the ``published`` branch of
          :meth:`visible_on_site`, which is the one branch that does not
          require a snapshot);
        * ``live_i18n`` is non-empty but lacks this language - published in
          other languages only, so there is no public revision *here*.
          Excluded, with no cross-language fallback.
        """
        return (
            Q(live_i18n__has_key=language_code)
            | Q(live_i18n={})
            | Q(live_i18n__isnull=True)
        )

    def visible_on_site(self):
        """
        Public visibility filter, language-agnostic half.

        Beta 11.11D1 replaced the previous proof of a past publication -
        ``last_published_revision_id IS NOT NULL`` - with ``is_published``
        plus a non-empty live snapshot, for two independent reasons found in
        the Beta 11.11C4J-R3 audit:

        * ``last_published_revision_id`` is a *legacy* marker holding a
          ``reversion.Version`` id, and only ``core.admin``'s publish action
          ever writes it - the editorial-view publish path does not, so a
          perfectly ordinary publish could leave a row that this filter would
          later drop off the site the moment it was edited;
        * it says nothing about whether the content was *withdrawn* again.

        ``is_published`` is written by every type's ``on_after_publish()`` and
        cleared by ``archive()`` (and by ``core.admin``'s archive/restore
        actions), so it tracks "is this content currently meant to be public"
        across both publish paths. Requiring a non-empty ``live_i18n`` on top
        keeps the widened branch fail-closed: a row that claims to be
        published but has nothing to serve stays offline rather than falling
        back to unreviewed draft fields.

        ``published`` itself is admitted unconditionally, exactly as before -
        records predating the snapshot mechanism must keep working.
        ``archived`` is admitted by neither branch.
        """
        return (
            self.filter(
                Q(status=EditorialWorkflowMixin.STATUS_PUBLISHED)
                | (
                    Q(status__in=self.LIVE_EDITING_STATUSES)
                    & Q(is_published=True)
                    & ~Q(live_i18n={})
                    & Q(live_i18n__isnull=False)
                )
            ).order_by("updated_at")
        )


class EditorialManager(TranslatableManager.from_queryset(EditorialQuerySet)):  # type: ignore
    """
    Translatable manager that exposes EditorialQuerySet; centralizes editorial filters for all content models.
    """


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

    STATUS_CHOICES = (
        (STATUS_DRAFT, "Draft"),
        (STATUS_REVIEW, "Review"),
        (STATUS_REWORK, "Rework"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_PUBLISHED, "Published"),
        (STATUS_ARCHIVED, "Archived"),
    )
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

    #: LEGACY MARKER - despite its name this holds a ``reversion.Version.id``,
    #: not a ``reversion.Revision.id``, and it is a plain IntegerField with no
    #: FK semantics at all (see core.admin.set_last_published_revision()).
    #:
    #: It predates the review binding below and means something different:
    #: "this object was published at least once", used by
    #: :meth:`EditorialQuerySet.visible_on_site` as the live-revision marker.
    #: It is deliberately NOT renamed, reinterpreted, converted to a Revision
    #: id, or used to seed :attr:`review_revision` - Beta 11.11B2A leaves its
    #: value and its meaning exactly as found.
    last_published_revision_id = models.IntegerField(null=True, blank=True)

    # ------------------------------------------------------------------
    # Review binding (Beta 11.11B2A) - schema only, not yet written to.
    #
    # These three are internal workflow metadata: never editable through a
    # ModelForm, never shown in a Parler language tab or an inline, and not
    # part of any public or API projection. Nothing in the runtime sets them
    # yet - submit, approve and publish leave all three untouched, which the
    # B2A test suite asserts. The binding logic, the fingerprint builder, the
    # publish guard and the invalidation follow in later slices.
    #
    # Both FKs point at ``reversion.Revision`` - the whole revision, i.e. the
    # complete graph the Beta 11.11B1 manifest now records (parent, children
    # and every translation). That is deliberately a different object from
    # ``last_published_revision_id`` above, which stores a ``Version.id``
    # (one row within one revision). Confusing the two would bind a review to
    # a single serialized row instead of the reviewed content as a whole.
    # ------------------------------------------------------------------

    #: The revision that was submitted for review. NULL for drafts and for
    #: every pre-B2A row: historical review/approved states carry no provable
    #: binding, and B2A refuses to invent one (see the data migrations).
    #:
    #: SET_NULL rather than CASCADE or PROTECT: reversion housekeeping
    #: (``deleterevisions``) must never delete editorial content, and must
    #: never be blocked by it either. Losing the binding degrades to
    #: "unbound", which the later publish guard treats as fail-closed.
    review_revision = models.ForeignKey(
        "reversion.Revision",
        null=True,
        blank=True,
        editable=False,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    #: The revision that was actually approved. Will be set to the same
    #: revision as :attr:`review_revision` at approval time; B2A implements no
    #: runtime invariant tying them together yet.
    approved_revision = models.ForeignKey(
        "reversion.Revision",
        null=True,
        blank=True,
        editable=False,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    #: SHA-256 hex digest of the reviewed editorial payload, once a later
    #: slice computes one. Exactly 64 characters when set; ``""`` means "not
    #: bound yet" and is the only value B2A ever stores. Not nullable, so
    #: "unbound" has a single representation rather than two.
    review_payload_fingerprint = models.CharField(
        max_length=64,
        blank=True,
        default="",
        editable=False,
    )

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
            logger.exception("Failed to save live_i18n snapshot for %r", self)

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
            self.review_note = note

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
            logger.exception("on_after_publish() hook failed for %r", self)

    def on_after_publish(self) -> None:
        """
        Post-publish extension point for subclasses (e.g., cache busting, search indexing).
        """

    @transition(field=status, source="*", target=STATUS_ARCHIVED)
    def archive(self, *, by, note=""):
        """
        Soft-delete: marks content as archived without destroying history;
        excluded from public queries.

        Beta 11.11D1 also clears :attr:`is_published` here. ``core.admin``'s
        ``action_archive`` already did that alongside the transition, but the
        editorial view calls this transition bare, so the two paths disagreed
        on what "archived" means for the publication flag. That inconsistency
        became load-bearing once D1 made ``is_published`` the proof of a past
        publication in
        :meth:`~core.models.editorial.EditorialQuerySet.visible_on_site`:
        without this line, archiving through the editorial view and then
        restoring would return the row to ``draft`` still carrying
        ``is_published=True`` and its old snapshot, silently republishing
        content that had been deliberately withdrawn. Archiving *is* the
        withdrawal, so the flag belongs to the transition rather than to one
        of its two callers.
        """
        self.is_published = False
        if note:
            self.review_note = note

    @transition(field=status, source=STATUS_ARCHIVED, target=STATUS_DRAFT)
    def restore(self, *, by, note=""):
        """
        Reverses archive to make content available to the workflow again.
        """
        if note:
            self.review_note = note

    # --- Internal review-invalidation transition (Beta 11.11B2B2/D1) ---
    #
    # Not a public workflow action: no admin action calls it, no view calls
    # it, and it takes no `by`/`note` arguments the way the transitions above
    # do. The sole caller is
    # `core.review_binding.invalidate_editorial_review_state()`, which runs it
    # on a freshly `select_for_update()`-locked row so the FSM state change
    # goes through a real django-fsm transition rather than a bare attribute
    # assignment (`status` is `protected=True`; direct assignment raises
    # `AttributeError`).
    #
    # Beta 11.11D1 removed its sibling `_invalidate_review_to_rework`. That
    # method existed solely to route an automatic invalidation to `rework`
    # when a live snapshot was present, which D1 abolished: `rework` is now
    # produced exclusively by the explicit `request_rework` transition above,
    # and staying public is decided by `EditorialQuerySet.visible_on_site()`
    # instead. With B2B2 as its only caller, dropping the transition also
    # removes the last runtime path that could reach `rework` automatically -
    # the property `prompts/tests/test_d1_draft_and_visibility.py` asserts.
    # The historical Beta 11.11B2A data migrations that once set `rework` on
    # existing rows are untouched; they carry their own frozen logic.
    #
    # Deliberately does nothing beyond the state change: no metadata clearing,
    # no `save()`, no revision, no other side effect. Clearing
    # `review_revision`/`approved_revision`/`review_payload_fingerprint`/
    # `reviewed_by`/`reviewed_at`/`submitted_for_review_at` and persisting the
    # result happens once, in the caller - keeping "what changes" in exactly
    # one place.

    @transition(field=status, source=[STATUS_REVIEW, STATUS_APPROVED], target=STATUS_DRAFT)
    def _invalidate_review_to_draft(self):
        """FSM-only state change for an automatically invalidated
        review/approved row. See the section docstring above."""
