"""Django settings for one app running in its own container.

Everything comes from the environment the supervisor sets; see
:mod:`bdh_sdk.runtime`.
"""
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

from . import app_spec

SPEC = app_spec()
APP_ID = SPEC.id
DATA_DIR = Path(os.environ.get("BDH_DATA_DIR", "/data"))

_token = os.environ.get("BDH_APP_TOKEN", "")
# Stable across restarts (signed-cookie sessions must survive them), unique per app token.
SECRET_KEY = os.environ.get("BDH_SECRET_KEY") or (
    hmac.new(_token.encode(), b"bdh-runtime-secret", hashlib.sha256).hexdigest()
    if _token else secrets.token_hex(32)
)
DEBUG = os.environ.get("BDH_DEBUG") == "1"
# Only the hub proxy can reach the container, and every request is signed by it.
ALLOWED_HOSTS = ["*"]


def _optional(module):
    try:
        __import__(module)
    except ImportError:
        return []
    return [module]


INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    *_optional("crispy_forms"),
    *_optional("crispy_bootstrap5"),
    "bdh_sdk.runtime.apps.RuntimeConfig",
    SPEC.app,
]
CRISPY_ALLOWED_TEMPLATE_PACKS = "bootstrap5"
CRISPY_TEMPLATE_PACK = "bootstrap5"

MIDDLEWARE = [
    "bdh_sdk.runtime.middleware.HealthMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "bdh_sdk.identity.IdentityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
]

# Django's default "same-origin" strips the Referer from cross-origin requests, and
# OpenStreetMap's tile servers answer those with an "Access blocked" tile.
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"

ROOT_URLCONF = "bdh_sdk.runtime.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.template.context_processors.media",
                "bdh_sdk.runtime.context_processors.hub",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATA_DIR / "app.sqlite3",
        "OPTIONS": {
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
            "transaction_mode": "IMMEDIATE",
        },
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Cookies are shared by every port on the hub's host, so the names carry the app id
# (the hub proxy only forwards and accepts cookies with this app's prefix).
COOKIE_PREFIX = f"bdh_{APP_ID.replace('-', '_')}_"
SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"
SESSION_COOKIE_NAME = COOKIE_PREFIX + "session"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_NAME = COOKIE_PREFIX + "csrf"
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("BDH_TIME_ZONE", "UTC")
USE_I18N = False
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = Path(os.environ.get("BDH_STATIC_ROOT", "/tmp/bdh-static"))
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
# Serve straight from the installed packages; there's no collectstatic step.
WHITENOISE_USE_FINDERS = True
WHITENOISE_AUTOREFRESH = False
MEDIA_URL = "/media/"
MEDIA_ROOT = DATA_DIR / "media"
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024

HUB_BRAND = json.loads(os.environ.get("BDH_HUB_BRAND") or "{}")
# No X-Frame-Options: the page lives in the hub's iframe, and the hub proxy sets
# CSP frame-ancestors instead.

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": os.environ.get("BDH_LOG_LEVEL", "INFO")},
}
