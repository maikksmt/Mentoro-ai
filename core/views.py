from core.seo.context import defaults
from core.seo.types import SeoMeta, AltHref


class SeoMixin:
    def build_seo(self, request, *, title, description, canonical, og_type=None, og_image=None, alternates=None, json_ld=None):
        alts: list[AltHref] = []
        for a in (alternates or []):
            if isinstance(a, AltHref):
                alts.append(a)
            else:
                alts.append(AltHref(**a))
        return SeoMeta(
            title=title, description=description, og_type=og_type, canonical=canonical,
            og_image=og_image, alternates=alts, json_ld=json_ld
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        if "seo" not in ctx:
            ctx["seo"] = defaults(self.request)
        return ctx
