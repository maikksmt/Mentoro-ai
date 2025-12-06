from django.urls import path

from content.views.legal import LegalNoticeView, PrivacyPolicyView, CookiePolicyView, TermsOfUseView, CopyrightView

app_name = "legal"

urlpatterns = [
    path("legal-notice/", LegalNoticeView.as_view(), name="legal-notice", ),
    path("privacy/", PrivacyPolicyView.as_view(), name="privacy", ),
    path("cookies/", CookiePolicyView.as_view(), name="cookies", ),
    path("terms/", TermsOfUseView.as_view(), name="terms-of-use", ),
    path("copyright/", CopyrightView.as_view(), name="copyright", ),
]
