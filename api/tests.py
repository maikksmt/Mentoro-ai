from django.test import TestCase
from django.urls import resolve
from django.urls.exceptions import Resolver404
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from catalog.models import Tool
from api.views import ToolSerializer, ToolViewSet

factory = APIRequestFactory()


class ToolViewSetWiringTests(TestCase):
    """
    Declarative contract of the (currently unmounted - see mentoroai/urls.py,
    the "api/" include is commented out) DRF wiring.
    """

    def test_viewset_is_read_only_and_uses_tool_queryset(self):
        self.assertEqual(ToolViewSet.queryset.model, Tool)
        self.assertEqual(ToolViewSet.serializer_class, ToolSerializer)
        self.assertFalse(hasattr(ToolViewSet, "create"))
        self.assertFalse(hasattr(ToolViewSet, "update"))
        self.assertFalse(hasattr(ToolViewSet, "destroy"))

    def test_serializer_meta_declares_expected_field_names(self):
        # Was "short_desc" - a typo that doesn't match any real Tool field
        # (catalog/models.py's translated field is "short_description") and
        # made every real GET raise ImproperlyConfigured; fixed in
        # api/views.py, see ToolApiDispatchTests below for the now-passing
        # contract.
        self.assertIs(ToolSerializer.Meta.model, Tool)
        self.assertEqual(
            ToolSerializer.Meta.fields,
            ["id", "name", "slug", "short_description", "free_tier", "rating"],
        )


class ToolApiUrlResolutionTests(TestCase):
    """
    api.urls is not included from mentoroai/urls.py (the "api/" path there
    is commented out), so its router is resolved directly against its own
    urlconf module rather than through the live site's ROOT_URLCONF.
    """

    def test_list_path_resolves_to_tool_viewset(self):
        match = resolve("/tools/", urlconf="api.urls")
        self.assertIs(match.func.cls, ToolViewSet)
        # ViewSetMixin.as_view() adds a "head" -> same-action mapping the
        # first time a view is dispatched, mutating this actions dict in
        # place (rest_framework/viewsets.py:106-107) - only the "get"
        # mapping is stable regardless of what has dispatched before us.
        self.assertEqual(match.func.actions["get"], "list")

    def test_detail_path_resolves_to_tool_viewset(self):
        match = resolve("/tools/1/", urlconf="api.urls")
        self.assertIs(match.func.cls, ToolViewSet)
        self.assertEqual(match.func.actions["get"], "retrieve")

    def test_unknown_path_does_not_resolve(self):
        with self.assertRaises(Resolver404):
            resolve("/does-not-exist/", urlconf="api.urls")


class ToolApiDispatchTests(TestCase):
    """
    Exercises the resolved views' dispatch() directly (rather than through
    the full Django request/response cycle) so that the assertions below
    are about this app's DRF contract, not about how the project's global
    error templates happen to render under a foreign urlconf.
    """

    @classmethod
    def setUpTestData(cls):
        cls.tool = Tool.objects.create(slug="tool-one", published_at=timezone.now())
        cls.tool.create_translation("en", name="Tool One", short_description="s")

    def test_post_to_list_is_rejected(self):
        # ReadOnlyModelViewSet has no "create" action, so the router never
        # wires POST to anything for this path.
        view = resolve("/tools/", urlconf="api.urls").func
        request = factory.post("/tools/", {"slug": "new"})
        response = view(request)

        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.accepted_media_type, "application/json")

    def test_detail_for_missing_id_404s(self):
        view = resolve("/tools/999999/", urlconf="api.urls").func
        request = factory.get("/tools/999999/")
        response = view(request, pk=999999)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data, {"detail": "No Tool matches the given query."})

    def test_list_action_returns_the_expected_tool_data(self):
        view = resolve("/tools/", urlconf="api.urls").func
        request = factory.get("/tools/")

        response = view(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.accepted_media_type, "application/json")
        self.assertEqual(len(response.data), 1)

        payload = response.data[0]
        self.assertEqual(
            set(payload.keys()),
            {"id", "name", "slug", "short_description", "free_tier", "rating"},
        )
        self.assertEqual(payload["id"], self.tool.pk)
        self.assertEqual(payload["name"], "Tool One")
        self.assertEqual(payload["slug"], "tool-one")
        self.assertEqual(payload["short_description"], "s")
        self.assertNotIn("short_desc", payload)

    def test_detail_action_returns_the_expected_tool_data(self):
        view = resolve(f"/tools/{self.tool.pk}/", urlconf="api.urls").func
        request = factory.get(f"/tools/{self.tool.pk}/")

        response = view(request, pk=self.tool.pk)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(response.data.keys()),
            {"id", "name", "slug", "short_description", "free_tier", "rating"},
        )
        self.assertEqual(response.data["short_description"], "s")
        self.assertNotIn("short_desc", response.data)
