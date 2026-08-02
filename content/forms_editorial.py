from django import forms
from django.apps import apps
from django.utils.translation import gettext_lazy as _

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


#: The one target status that carries an editorial reason. Sending content
#: back is the only workflow action where the *why* is the whole point, and
#: the only one whose transition
#: (``EditorialWorkflowMixin.request_rework``) assigns ``review_note``
#: unconditionally - so an empty value there does not merely skip writing a
#: note, it wipes whatever reason was on the row.
REWORK_STATUS = "rework"


class ReviewUpdateForm(forms.Form):
    model = forms.ChoiceField(choices=MODEL_CHOICES)
    object_id = forms.IntegerField(min_value=1)
    status = forms.ChoiceField(choices=STATUS_CHOICES)

    #: Beta 11.13D1G-a. Plain text, never rich text: it is rendered with
    #: Django's ordinary autoescaping and is never passed through a sanitiser
    #: or ``mark_safe``. ``CharField`` strips surrounding whitespace, so a
    #: whitespace-only submission arrives here as ``""`` and is rejected by
    #: :meth:`clean` below rather than silently clearing the stored reason.
    #:
    #: No ``max_length``: the model field is a ``TextField`` with no limit of
    #: its own, and inventing one here would reject content the database
    #: accepts.
    review_note = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        label=_("Reason for rework"),
        help_text=_("Required when requesting rework."),
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean(self):
        """
        Require a reason for rework, and only for rework.

        Enforced here rather than in the view or the template so that every
        surface posting this form is covered - the review queue renders the
        field, but ``my_content_update`` accepts the same payload and must not
        become a way around the requirement. An HTML ``required`` attribute
        alone would be no protection at all.
        """
        cleaned = super().clean()
        status = cleaned.get("status")
        note = (cleaned.get("review_note") or "").strip()

        if status == REWORK_STATUS:
            if not note:
                self.add_error(
                    "review_note",
                    _("Please give a short reason so the author knows what to change."),
                )
            else:
                cleaned["review_note"] = note
        else:
            # Every other action leaves ``review_note`` alone; carrying a value
            # forward would let an unrelated transition overwrite the last
            # editorial reason.
            cleaned["review_note"] = ""
        return cleaned
