"""Django settings for E2E tests — uses file-based SQLite shared with test process."""

import os
import tempfile
from pathlib import Path

# Ensure a stable DB directory shared between test process and dev-server subprocess.
# The first process to import this module creates the temp dir and publishes it via
# env var; subsequent imports (e.g. the runserver subprocess) reuse it.
_DB_DIR = Path(os.environ.get("TEATREE_E2E_DB_DIR") or tempfile.mkdtemp(prefix="teatree-e2e-"))
_DB_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("TEATREE_E2E_DB_DIR", str(_DB_DIR))
E2E_DB_PATH = _DB_DIR / "e2e.sqlite3"

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
    "django_htmx",
    "django_tasks",
    "django_tasks_db",
    "teatree.core",
    "teatree.agents",
]

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
