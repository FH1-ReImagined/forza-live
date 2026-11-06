"""Request rules for the whole service.

ApiHeaders: every /api/ answer is no-store and nosniff, and carries no CORS header; OPTIONS is
405; the launcher's calls (/api/game/) also get the server's clock (X-Forza-Time). The public
like totals (/api/likes/counts) and Photo of the Month's standings and winners (/api/potm/...),
the same for everybody, keep the short public cache their views set.
AdminOnlyFromAllowed: the admin (and the API docs, when on) answer only FORZA_ADMIN_ALLOW.
AuthRoutes: of allauth's routes only the ones the sign-ins use exist (no password, signup or email
management routes). FetchSiteCheck: a change (POST, PUT, PATCH, DELETE) from a browser that says
it is not same-origin is refused, on top of Django's CSRF token and Origin checks, and our own
API's changes must be JSON (allauth's sign-in button is a plain form, which its CSRF token guards),
but for a hub upload's PUT of the zip itself (members.hub checks its type).
SessionLifetime: a session ends 30 days after its last use (refreshed at most once a day) and 90
days after its sign-in.
"""

import re
import time

from django.conf import settings
from django.contrib.auth import logout
from django.http import Http404, JsonResponse

from members import net

UNSAFE = ("POST", "PUT", "PATCH", "DELETE")
HEADLESS = "/api/auth/browser/v1/"
HEADLESS_ROUTES = {
    "config": ("GET",),
    "auth/session": ("GET", "DELETE"),
    "auth/code/request": ("POST",),
    "auth/code/confirm": ("POST",),
    "auth/provider/redirect": ("POST",),
}
CALLBACK = re.compile(r"/api/auth/(discord|github)/login/callback/")
PUBLIC_CACHE = ("/api/likes/counts", "/api/potm/standings", "/api/potm/winners", "/api/hub/items")
PUBLIC_ITEM = re.compile(r"/api/hub/items/[A-Z2-7]{12}")   # one hub item (members.hub)
UPLOAD = re.compile(r"/api/hub/items/[A-Z2-7]{12}/file")    # a hub upload: the zip itself, not JSON


class ApiHeaders:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith("/api/") and request.method == "OPTIONS":
            response = JsonResponse({"error": "method not allowed"}, status=405)
        else:
            response = self.get_response(request)
        if request.path.startswith("/api/game/"):
            response["X-Forza-Time"] = str(int(time.time()))  # the launcher corrects its clock by it
        if request.path.startswith("/api/"):
            if not ((request.path in PUBLIC_CACHE or PUBLIC_ITEM.fullmatch(request.path))
                    and response.status_code in (200, 304)
                    and response.get("Cache-Control", "").startswith("public,")):
                response["Cache-Control"] = "no-store"
            response["X-Content-Type-Options"] = "nosniff"
            for name in [h for h in response.headers if h.lower().startswith("access-control-")]:
                del response[name]
        return response


class AdminOnlyFromAllowed:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path
        if (path.startswith("/admin/") or path in ("/api/docs", "/api/openapi.json")) and not net.admin_allowed(request):
            raise Http404()
        return self.get_response(request)


class AuthRoutes:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path
        if path.startswith("/api/auth/"):
            if path.startswith(HEADLESS):
                methods = HEADLESS_ROUTES.get(path[len(HEADLESS):])
                if methods is None:
                    return JsonResponse({"status": 404}, status=404)
                if request.method not in methods + ("HEAD",):
                    return JsonResponse({"status": 405}, status=405)
            elif not CALLBACK.fullmatch(path):
                return JsonResponse({"status": 404}, status=404)
        return self.get_response(request)


class FetchSiteCheck:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        site = request.headers.get("Sec-Fetch-Site")
        if request.method in UNSAFE and site is not None and site != "same-origin":
            return JsonResponse({"error": "refused"}, status=403)
        if (request.method in UNSAFE and request.path.startswith("/api/") and not request.path.startswith("/api/auth/")
                and request.content_type != "application/json"
                and not (request.method == "PUT" and UPLOAD.fullmatch(request.path))):
            return JsonResponse({"error": "send JSON"}, status=415)
        return self.get_response(request)


class SessionLifetime:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            now = int(time.time())
            started = request.session.get("fh_t0")
            if not isinstance(started, int):
                request.session["fh_t0"] = started = now
            if now - started > settings.SESSION_MAX_DAYS * 86400:
                logout(request)
            elif request.session.get("fh_day") != now // 86400:
                request.session["fh_day"] = now // 86400  # saving it renews the cookie's 30 days
        return self.get_response(request)
