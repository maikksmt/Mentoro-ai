from django.urls import path

from .views import GlossaryAutocompleteView, GlossaryDetailView, GlossaryListView

app_name = "glossary"

urlpatterns = [
    path("", GlossaryListView.as_view(), name="list"),
    path(
        "autocomplete/", GlossaryAutocompleteView.as_view(), name="autocomplete"
    ),  # HTMX
    path("<slug:slug>/", GlossaryDetailView.as_view(), name="detail"),
]
