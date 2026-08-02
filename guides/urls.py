from django.urls import path

from .views import GuideDetailView, GuideListView

app_name = "guides"

urlpatterns = [
    path("", GuideListView.as_view(), name="list"),
    path("<slug:slug>/", GuideDetailView.as_view(), name="detail"),
]
