from django.urls import path

from .views import ComparisonDetailView, ComparisonListView

app_name = "compare"

urlpatterns = [
    path("", ComparisonListView.as_view(), name="index"),
    path("<slug:slug>/", ComparisonDetailView.as_view(), name="detail"),
]
