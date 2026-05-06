"""Django settings for E2E tests — uses file-based SQLite, one per xdist worker."""

import os
import tempfile
from pathlib import Path

# Ensure a stable DB directory shared between test process and dev-server subprocess.
# The first process to import this module creates the temp dir and publishes it via
# env var; subsequent imports (e.g. the runserver subprocess) reuse it.
_DB_DIR = Path(os.environ.get("TEATREE_E2E_DB_DIR") or tempfile.mkdtemp(prefix="teatree-e2e-"))
_DB_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("TEATREE_E2E_DB_DIR", str(_DB_DIR))

# Per-worker DB when running under pytest-xdist (PYTEST_XDIST_WORKER = "gw0", "gw1", …).
# Without xdist the env var is absent → single shared DB.
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "")
E2E_DB_PATH = _DB_DIR / f"e2e_{_WORKER}.sqlite3" if _WORKER else _DB_DIR / "e2e.sqlite3"

SECRET_KEY = "e2e-tests-only"
DEBUG = True
ALLOWED_HOSTS = ["*"]
ROOT_URLCONF = "tests.urls"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(E2E_DB_PATH),
    },
}

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.messages",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "django_htmx",
    "django_tasks",
    "django_tasks_db",
    "teatree.core",
    "teatree.agents",
]

STATIC_URL = "static/"
TIME_ZONE = "UTC"
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

TASKS = {
    "default": {
        "BACKEND": "django_tasks.backends.immediate.ImmediateBackend",
    },
}

# File-based cache shared across the test process and the live ASGI subprocess.
# The default LocMemCache is per-process, so seeding ``PENDING_REVIEWS_CACHE_KEY``
# from conftest would never reach the server's panel builder.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
        "LOCATION": str(_DB_DIR / "cache"),
    },
}

# Disable the dashboard auto-sync POST in e2e mode. The auto-sync's
# ``hx-on::after-request="refreshPanels"`` triggers a second wave of panel
# reloads after the initial HTMX panel loads have already settled, which can
# swap DOM nodes underneath in-flight test interactions. Tests don't need
# sync to run.
TEATREE_DASHBOARD_AUTO_SYNC = False
