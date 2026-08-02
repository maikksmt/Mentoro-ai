from core.seo.context import defaults
from core.seo.types import AltHref, SeoMeta


class SeoMixin:
    NOINDEX_QUERY_KEYS = frozenset({
        "category",
        "page",
        "letter",
        "q",
        "search",
        "sort",
    })

    def _robots_for_request(self, request) -> str:
        if request.path.startswith("/admin/"):
            return "noindex,nofollow"

        if any(k in request.GET for k in self.NOINDEX_QUERY_KEYS):
            return "noindex,follow"

        return "index,follow"

    def _canonical_for_request(self, request, canonical: str, robots: str) -> str:
        # Wenn wir wegen Query noindex setzen: canonical ohne Query-String erzwingen
        # Beispiel: /de/catalog/?category=x -> canonical = /de/catalog/
        if robots == "noindex,follow" and request.GET:
            return request.build_absolute_uri(request.path)
        return canonical

    def build_seo(self, request, *, title, description, date=None, author=None, canonical, og_type=None, og_image=None, alternates=None,
                  json_ld=None, robots=None, ):
        alts: list[AltHref] = []
        if date is not None:
            date = date.strftime("%Y-%m-%dT%H:%M:%S")
        for a in (alternates or []):
            if isinstance(a, AltHref):
                alts.append(a)
            else:
                alts.append(AltHref(**a))
        if robots is None:
            robots = self._robots_for_request(request)

        canonical = self._canonical_for_request(request, canonical, robots)

        return SeoMeta(
            title=title, description=description, date=date, author=author,
            og_type=og_type, canonical=canonical, robots=robots,
            og_image=og_image, alternates=alts, json_ld=json_ld
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        if "seo" not in ctx:
            ctx["seo"] = defaults(self.request)
        return ctx
