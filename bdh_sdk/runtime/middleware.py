"""Runtime middleware that has to run before the identity check."""
from django.http import JsonResponse

HEALTH_PATH = "/_bdh/health"


class HealthMiddleware:
    """Answer the supervisor's health probe without a hub signature.

    It reveals nothing but liveness, and the container is only reachable by
    the hub and the supervisor.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == HEALTH_PATH and request.method in ("GET", "HEAD"):
            from . import app_spec

            return JsonResponse({"ok": True, "app": app_spec().id})
        return self.get_response(request)
