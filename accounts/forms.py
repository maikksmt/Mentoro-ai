from django import forms
from django.contrib.auth import get_user_model

User = get_user_model()


class UserAccountForm(forms.ModelForm):
    first_name = forms.CharField(
        max_length=20,
        widget=forms.TextInput(attrs={
            "class": "input input-bordered w-full",
            "placeholder": "Vorname",
        })
    )

    last_name = forms.CharField(
        max_length=30,
        widget=forms.TextInput(attrs={
            "class": "input input-bordered w-full",
            "placeholder": "Nachname",
        })
    )

    class Meta:
        model = User
        fields = ("first_name", "last_name")
