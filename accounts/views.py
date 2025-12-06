# accounts/views.py
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView

from core.seo.utils import absolute_url, get_og_image
from core.views import SeoMixin
from .forms import UserAccountForm


class AccountDashboardView(LoginRequiredMixin, SeoMixin, TemplateView):
    template_name = "account/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        canonical = absolute_url(self.request.path)
        title = _("My account")
        description = _(
            "Manage your MentoroAI profile, settings and editorial access."
        )
        context["seo"] = self.build_seo(
            self.request,
            title=title,
            description=description,
            canonical=canonical,
            og_image=get_og_image(),
            alternates=None,
            json_ld={
                "@context": "https://schema.org",
                "@type": "ProfilePage",
                "name": title,
                "description": description,
                "url": canonical,
                "inLanguage": getattr(self.request, "LANGUAGE_CODE", "en"),
            },
        )

        context["user_account_form"] = UserAccountForm(instance=self.request.user)
        return context

    def post(self, request, *args, **kwargs):
        form = UserAccountForm(request.POST, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, _("Your profile has been updated."))
            return redirect("account_dashboard")

        context = self.get_context_data()
        context["user_account_form"] = form
        return self.render_to_response(context)
