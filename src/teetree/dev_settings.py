"""Standalone dev settings for running the TeaTree dashboard locally.

Usage::

    DJANGO_SETTINGS_MODULE=teetree.dev_settings t3 dashboard
    t3 --settings teetree.dev_settings dashboard
"""

from teetree.config import default_logging, get_data_dir

SECRET_KEY = "teatree-dev-insecure"  # noqa: S105
DEBUG = True
USE_TZ = True

ROOT_URLCONF = "teetree.core.urls"

_DATA_DIR = get_data_dir("dev")

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(_DATA_DIR / "db.sqlite3"),
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
    "django_rich",
    "django_tasks",
    "django_tasks_db",
    "teetree.core",
    "teetree.agents",
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

STATIC_URL = "static/"

LOGGING = default_logging("dev")

TEATREE_HEADLESS_RUNTIME = "claude-code"
TEATREE_INTERACTIVE_RUNTIME = "codex"
TEATREE_TERMINAL_MODE = "same-terminal"
TEATREE_CLAUDE_STATUSLINE_STATE_DIR = "/tmp/claude-statusline"  # noqa: S108
TEATREE_AGENT_HANDOVER = [
    {
        "runtime": "claude-code",
        "telemetry": {
            "provider": "claude-statusline",
            "switch_away_at_percent": 95,
            "switch_back_at_percent": 80,
        },
    },
    {
        "runtime": "codex",
    },
]
