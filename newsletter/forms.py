import time

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _


class SubscriptionForm(forms.Form):
    email = forms.EmailField(
        label=_("email"),
        widget=forms.EmailInput(
            attrs={
                "autocomplete": "email",
                "class": "input input-bordered w-full",
                "placeholder": "name@example.com",
            }
        ),
    )

    # Honeypot: muss leer bleiben
    company = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={
                "tabindex": "-1",
                "autocomplete": "off",
                "class": "hidden",
                "aria-hidden": "true",
            }
        ),
    )

    # Timestamp für Mindest-Ausfüllzeit
    form_rendered_at = forms.FloatField(
        required=False,
        widget=forms.HiddenInput(),
    )

    def __init__(self, *args, **kwargs):
        initial = kwargs.setdefault("initial", {})
        initial.setdefault("form_rendered_at", time.time())
        super().__init__(*args, **kwargs)

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()

    def clean(self):
        cleaned_data = super().clean()

        if cleaned_data.get("company"):
            raise ValidationError(_("Invalid submission."))

        rendered_at = cleaned_data.get("form_rendered_at")
        if rendered_at:
            elapsed = time.time() - rendered_at
            if elapsed < 1.5:
                raise ValidationError(_("Invalid submission."))

        return cleaned_data


class UnsubscribeForm(forms.Form):
    email = forms.EmailField(
        label=_("email"),
        widget=forms.EmailInput(
            attrs={
                "autocomplete": "email",
                "class": "input input-bordered w-full",
                "placeholder": "name@example.com",
            }
        ),
    )
    reason = forms.CharField(
        label=_("Reason (optional)"),
        required=False,
        widget=forms.TextInput(attrs={"class": "input input-bordered w-full"}),
    )

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()
