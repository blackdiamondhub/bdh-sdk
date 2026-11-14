from django.conf import settings
from django.urls import include, path, re_path
from django.views.static import serve

from . import app_spec, views

_spec = app_spec()

urlpatterns = [
    path("_bdh/hook", views.hook, name="bdh_hook"),
    path("_bdh/events", views.events, name="bdh_events"),
    path("hub_settings/app/<str:app_id>/", views.hub_settings_app, name="hub_settings_app"),
    path("hub/api/tts/", views.tts, name="hub_tts"),
    re_path(r"^media/(?P<path>.*)$", serve, {"document_root": settings.MEDIA_ROOT}),
]
if _spec.urls:
    urlpatterns.append(path(_spec.prefix, include(_spec.urls)))
