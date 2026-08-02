from django.db.models import Prefetch, Q
from django.http import Http404
from django.urls import reverse
from django.utils.translation import get_language
from django.utils.translation import gettext as _
from django.views.generic import DetailView, ListView

from core.models.editorial import EditorialWorkflowMixin
from core.seo.utils import absolute_url, get_og_image, localized_alternates, seo_text
from core.services import related_guides, to_teaser_item
from core.views import SeoMixin

from .models import Guide, GuideItem, GuideSection
from .presentation import public_display_sections


def _resolve_guide_by_slug(qs, slug: str, language_code: str) -> Guide | None:
    """
    Beta 8.10a: once a guide has a live_i18n snapshot for language_code,
    that snapshot's slug is the SOLE public slug for that language - the
    current (possibly draft-in-progress) translation slug must never
    resolve as an additional alias. Without this, a guide mid-revision
    would immediately expose its new, unpublished slug (and therefore its
    new title/body) before the revision is ever published, since the
    translation row's slug field is updated the moment an editor changes
    it, independent of publish status.

    Resolution order:
    1. live_i18n[language_code]'s public_slug/slug - single query via
       JSONField key lookup. If the guide has a live snapshot for this
       language, only this slug may resolve; a differing current-
       translation slug is not tried at all.
    2. A narrow backward-compatibility fallback to the current
       translation's public_slug/slug, but ONLY for guides that are
       strictly `published` AND have no live_i18n entry for this language
       at all (a historical record predating the Beta 8.6 live-snapshot
       mechanism). Review/approved guides without a live snapshot for this
       language are deliberately excluded from this fallback - they must
       never leak an unpublished slug just because a snapshot happens to
       be missing; visible_on_site() already requires a genuine live
       revision for those statuses.

    Language matching is scoped throughout (translations__language_code=
    language_code), so a bilingual guide's slug in the other language can
    never resolve under this prefix either.
    """
    live_match = (
        qs.filter(
            Q(**{f"live_i18n__{language_code}__public_slug": slug})
            | Q(**{f"live_i18n__{language_code}__slug": slug})
        )
        .distinct()
        .first()
    )
    if live_match:
        return live_match

    compat_qs = (
        qs.filter(status=EditorialWorkflowMixin.STATUS_PUBLISHED)
        .exclude(live_i18n__has_key=language_code)
    )
    return (
        compat_qs.filter(
            Q(translations__language_code=language_code, translations__public_slug=slug)
            | Q(translations__language_code=language_code, translations__slug=slug)
        )
        .distinct()
        .first()
    )


class GuideListView(SeoMixin, ListView):
    paginate_by = 15
    template_name = "guides/guide_list.html"
    context_object_name = "object_list"

    def get_queryset(self):
        lang = get_language()
        # Beta 8.10: visible_in_language() (strict, no cross-language
        # fallback) instead of .visible_on_site().active_translations() -
        # every card's detail URL must actually resolve under the active
        # language (see GuideDetailView's strict slug match, hardened
        # alongside this).
        return (
            Guide.objects.visible_in_language(lang)
            .select_related("author", "reviewed_by")
            .prefetch_related("categories__translations", "tools__translations")
            .ordered_for_listing()
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        lang = get_language()
        canonical = absolute_url(self.request.path)
        title = _("AI guides · MentoroAI")
        description = _(
            "Browse practical AI guides and tutorials to learn step by step how to use AI tools in everyday life and work."
        )
        alts = localized_alternates(self.request, "guides:list")
        ctx["seo"] = self.build_seo(
            self.request,
            title=title,
            description=description,
            canonical=canonical,
            alternates=alts,
            json_ld={
                "@context": "https://schema.org",
                "@type": "CollectionPage",
                "name": title,
                "description": description,
                "url": canonical,
                "inLanguage": lang,
            },
        )
        ctx["crumbs"] = [(_("Guides"), reverse("guides:list"))]
        return ctx


class GuideDetailView(SeoMixin, DetailView):
    model = Guide
    template_name = "guides/guide_detail.html"
    context_object_name = "object"
    slug_field = "slug"
    slug_url_kwarg = "slug"

    def get_object(self, queryset=None):
        # Beta 8.10: resolve through self.get_queryset() (visible_in_language(),
        # strict status + language) instead of the previous unguarded
        # Guide.objects.filter(...)/Guide.objects.all() lookups - a draft was
        # publicly resolvable by slug, and a wrong-language slug matched
        # regardless of the active prefix, both empirically confirmed.
        slug = self.kwargs["slug"]
        lang = get_language()
        qs = queryset or self.get_queryset()
        obj = _resolve_guide_by_slug(qs, slug, lang)
        if not obj:
            raise Http404("Guide not found.")
        return obj

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        obj: Guide = ctx["object"]
        lang = get_language()
        # Beta 8.10: display_title/display_intro/display_body (live-snapshot
        # first, current translation second) instead of the raw obj.title/
        # obj.intro/obj.body Parler descriptors - the raw properties reflect
        # whatever is currently in the translation row, which during an
        # in-progress review/approved revision is the NEW unpublished draft
        # text, not the last published live version. The page body already
        # used display_* below; only the SEO title/description/json_ld
        # headline read the raw (live-revision-unsafe) properties.
        title = f"{obj.display_title}"
        desc_source = obj.display_intro or obj.display_body or obj.display_title
        desc = seo_text(desc_source)[:155]
        canonical = absolute_url(self.request.path)
        og_img = getattr(obj, "hero_image_url", None)
        author_obj = getattr(obj, "author", None)
        author_name = ""
        if author_obj:
            author_name = (author_obj.get_full_name() or getattr(author_obj, "username", "") or "")
        alts = localized_alternates(
            self.request,
            url_name="guides:detail",  # optional, Fallback
            obj=obj,
        )
        json_ld = {
            "@context": "https://schema.org",
            "@type": "Article",
            "headline": title,
            "description": desc,
            "url": canonical,
            "inLanguage": lang,
            "mainEntityOfPage": canonical,

        }
        if og_img:
            json_ld["image"] = [og_img]

        ctx["seo"] = self.build_seo(
            self.request,
            title=title,
            description=desc,
            date=obj.updated_at,
            author=author_name,
            og_type="article",
            canonical=canonical,
            og_image=get_og_image(og_img),
            alternates=alts,
            json_ld=json_ld,
        )
        ctx["display_title"] = obj.display_title
        ctx["display_intro"] = obj.display_intro
        ctx["display_body"] = obj.display_body
        # Beta 11.4: sections/items are resolved here instead of inline in
        # the template. Same prefetch caches, same snapshot-first model
        # accessors, same is_published gate - the rendered output is
        # unchanged. Moving it into an explicit context entry is what lets
        # the admin draft preview feed the very same template from the saved
        # draft instead (see guides/presentation.py); the public path never
        # sees a preview flag.
        ctx["display_sections"] = public_display_sections(obj)
        rel_qs = related_guides(obj, limit=3, language_code=lang)
        ctx["related_guides"] = [to_teaser_item(g, "guide") for g in rel_qs]
        ctx["crumbs"] = [
            (_("Guides"), reverse("guides:list")),
            (obj.display_title, self.request.path),
        ]
        return ctx

    def get_queryset(self):
        lang = get_language()
        # Beta 8.10: visible_in_language() instead of active_translations()
        # (which had no status filter at all here) - see get_object().
        return (
            Guide.objects.visible_in_language(lang)
            .select_related("author")
            .prefetch_related(
                "categories__translations",
                "tools__translations",
                Prefetch(
                    "sections",
                    queryset=(
                        GuideSection.objects
                        .active_translations(lang)
                        .order_by("order")
                        .prefetch_related(
                            Prefetch(
                                "items",
                                queryset=(
                                    GuideItem.objects
                                    .active_translations(lang)
                                    .filter(is_published=True)
                                    .order_by("order")
                                    .distinct()
                                )
                            )
                        )
                        .distinct()
                    )
                )
            )
        )
