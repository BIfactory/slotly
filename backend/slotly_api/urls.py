from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path

from apps.calendars.public_views import bridge_ics


def healthz(_request):
    return JsonResponse({"status": "ok", "service": "slotly-api"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz", healthz),
    # Public ICS bridge — lives at the root (not /api/) because Google
    # Calendar fetches it without authentication and expects a clean URL.
    path("ics/<str:token>.ics", bridge_ics, name="bridge-ics"),
    path("_allauth/", include("allauth.headless.urls")),
    path("api/", include("apps.accounts.urls")),
    path("api/", include("apps.calendars.urls")),
    path("api/", include("apps.teams.urls")),
    path("api/", include("apps.search.urls")),
    path("api/", include("apps.notifications.urls")),
    path("api/", include("apps.availability.urls")),
    path("api/", include("apps.connections.urls")),
    path("api/", include("apps.scheduling.urls")),
    path("mcp/v1/", include("apps.mcp.urls")),
]
