# content/views_legal.py
from django.utils.translation import get_language
from django.utils.translation import gettext as _
from django.views.generic import TemplateView

from core.seo.utils import absolute_url, get_og_image
from core.views import SeoMixin


class BaseLegalPageView(SeoMixin, TemplateView):
    """
    Basisklasse für statische Legal-Seiten mit SEO-Metadaten.
    """
    seo_title = ""
    seo_description = ""
    schema_type = "WebPage"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        lang = get_language() or "en"
        canonical = absolute_url(self.request.path)

        ctx["seo"] = self.build_seo(
            self.request,
            title=self.seo_title,
            description=self.seo_description,
            canonical=canonical,
            og_image=get_og_image(),
            alternates=None,  # Legal-Seiten meist nur in aktueller Sprache
            json_ld={
                "@context": "https://schema.org",
                "@type": self.schema_type,
                "name": self.seo_title,
                "description": self.seo_description,
                "url": canonical,
                "inLanguage": lang,
            },
        )
        return ctx


class LegalNoticeView(BaseLegalPageView):
    template_name = "legal/legal_notice.html"
    seo_title = _("Legal Notice – MentoroAI")
    seo_description = _(
        "Legal notice (Impressum) for MentoroAI with information according to § 5 TMG and contact details."
    )
    schema_type = "AboutPage"


class PrivacyPolicyView(BaseLegalPageView):
    template_name = "legal/privacy_policy.html"
    seo_title = _("Privacy Policy – MentoroAI")
    seo_description = _(
        "Privacy policy for MentoroAI explaining data processing, cookies, analytics and your rights under GDPR."
    )
    schema_type = "WebPage"


class CookiePolicyView(BaseLegalPageView):
    template_name = "legal/cookie_policy.html"
    seo_title = _("Cookie Policy – MentoroAI")
    seo_description = _(
        "Cookie policy for MentoroAI describing the types of cookies used, consent management and control options."
    )
    schema_type = "WebPage"


class TermsOfUseView(BaseLegalPageView):
    template_name = "legal/terms_of_use.html"
    seo_title = _("Terms of Use – MentoroAI")
    seo_description = _(
        "Terms of use for MentoroAI outlining scope, use of content, user accounts, liability and changes."
    )
    schema_type = "WebPage"


class CopyrightView(BaseLegalPageView):
    template_name = "legal/copyright.html"
    seo_title = _("Copyright & Licenses – MentoroAI")
    seo_description = _(
        "Information on copyright, open-source license (GPLv3) and use of content and trademarks for MentoroAI."
    )
    schema_type = "WebPage"
