from django import forms
from django.apps import apps

MODEL_NAMES = [
    ("comparison", "Comparison"),
    ("guide", "Guide"),
    ("usecase", "UseCase"),
    ("prompt", "Prompt"),
]

APP_ORDER = ["guides", "usecases", "prompts", "compare"]
MODEL_CHOICES = [(k, label) for k, label in MODEL_NAMES]
STATUS_CHOICES = [
    ("review", "Review"),
    ("rework", "Rework"),
    ("approved", "Approved"),
    ("published", "Published"),
    ("archived", "Archived"),
    ("draft", "Draft"),
]


def get_model_or_none(model_name):
    for app_label in APP_ORDER:
        try:
            return apps.get_model(app_label, model_name)
        except LookupError:
            continue
    return None


class SubmitToReviewForm(forms.Form):
    model = forms.ChoiceField(choices=MODEL_CHOICES)
    object_id = forms.IntegerField(min_value=1)

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user


class ReviewUpdateForm(forms.Form):
    model = forms.ChoiceField(choices=MODEL_CHOICES)
    object_id = forms.IntegerField(min_value=1)
    status = forms.ChoiceField(choices=STATUS_CHOICES)

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
