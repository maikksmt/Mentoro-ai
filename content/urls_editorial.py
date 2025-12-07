from django.urls import path
from django.views.generic import TemplateView

from .views import editorial as views

app_name = "editorial"

urlpatterns = [
    path("me/content/", views.my_content, name="my_content"),
    path("me/submit/", views.submit_to_review, name="submit_to_review"),
    path("me/update/", views.my_content_update, name="my_content_update"),
    path("review/", views.review_queue, name="review_queue"),
    path("review/update/", views.review_update, name="review_update"),
    path("layout-examples/", TemplateView.as_view(template_name="content/richtext_layout_examples.html"), name="layout_examples"),
]
