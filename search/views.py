"""
The public search page.

A thin shell around search_site(): it reads one parameter, calls the service
once and renders whatever comes back. Sorting, filtering, counting and
visibility all belong to the service and its adapters, and none of them are
repeated here.

Class-based with SeoMixin because every public page in this project is - that
is also what supplies the robots, canonical and hreflang handling this page
needs. TemplateView answers anything but GET/HEAD/OPTIONS with 405, so the
GET-only rule needs no extra decorator.
"""
from __future__ import annotations

import logging

from django.utils.translation import gettext as _
from django.views.generic import TemplateView

from core.seo.utils import absolute_url, get_og_image, localized_alternates
from core.views import SeoMixin
from search.exceptions import SearchExecutionError
from search.services import search_site

logger = logging.getLogger(__name__)


class SearchResultsView(SeoMixin, TemplateView):
    template_name = "search/results.html"

    def get(self, request, *args, **kwargs):
        raw_query = request.GET.get("q")
        # Distinguishes the first visit from a deliberately emptied search box:
        # both have no usable query, but only one of them is a mistake.
        has_query_parameter = "q" in request.GET

        search_response = None
        search_failed = False
        status = 200
        try:
            # The language comes from the resolved URL prefix, not from the
            # ambient translation state, so a /de/ request cannot answer in
            # English because something else activated it.
            search_response = search_site(
                raw_query=raw_query, language_code=request.LANGUAGE_CODE
            )
        except SearchExecutionError as error:
            # Deliberately narrow: only the search's own failure contract is
            # turned into a page. Anything else stays a 500.
            #
            # The page itself says nothing about why, so without this line a
            # failing adapter would be invisible in production. The message is
            # built only from values the code itself wrote - the adapter's kind
            # and the service's own fixed reason - so no search term and no SQL
            # can end up in it. The traceback carries the original database
            # error, which is the only place the root cause survives and never
            # reaches the visitor.
            logger.exception(
                "Global search failed for the %s adapter in %s: %s",
                error.kind,
                request.LANGUAGE_CODE,
                error.reason,
            )
            search_failed = True
            status = 503

        if search_response is not None:
            query_value = search_response.query.value
        else:
            query_value = raw_query or ""

        context = self.get_context_data(
            search_response=search_response,
            search_failed=search_failed,
            has_query_parameter=has_query_parameter,
            query_value=query_value,
            **kwargs,
        )
        return self.render_to_response(context, status=status)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        title = _("Search Mentoro AI")
        description = _(
            "Find practical AI tools, guides, prompts, use cases, and comparisons."
        )
        context["seo"] = self.build_seo(
            self.request,
            title=title,
            description=description,
            canonical=absolute_url(self.request.path),
            alternates=localized_alternates(self.request, "search:results"),
            og_image=get_og_image(),
            og_type="website",
            # Result pages are never worth indexing, with or without a query -
            # the automatic rule only reacts to a present `q`.
            robots="noindex,follow",
        )
        return context
