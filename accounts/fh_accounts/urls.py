"""Everything is under /api/, the one path the website's nginx sends here, except the admin,
which nginx does not pass on and members.middleware limits to FORZA_ADMIN_ALLOW."""

from allauth.headless.constants import Client
from django.contrib import admin
from django.urls import include, path

from members import likes  # noqa: F401  (likes and the player's own items, on members.api.api)
from members import links  # noqa: F401  (the game link's routes, on members.api.api)
from members import views
from members.api import api

HEADLESS = "api/auth/browser/v1/"

urlpatterns = [
    # allauth's headless views with the design's rules (members.views), ahead of allauth's own.
    path(HEADLESS + "auth/code/request", views.StandInRequestLoginCodeView.as_api_view(client=Client.BROWSER)),
    path(HEADLESS + "auth/code/confirm", views.CountedConfirmLoginCodeView.as_api_view(client=Client.BROWSER)),
    path(HEADLESS + "auth/provider/redirect", views.LimitedRedirectToProviderView.as_api_view(client=Client.BROWSER)),
    path("api/auth/", include("allauth.headless.urls")),
    # The providers' callbacks: /api/auth/{discord,github}/login/callback/.
    path("api/auth/", include("allauth.urls")),
    path("api/", api.urls),
    path("admin/", admin.site.urls),
]
