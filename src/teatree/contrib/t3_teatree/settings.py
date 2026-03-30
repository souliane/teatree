"""Django settings for the bundled t3-teatree overlay.

Used when teatree dogfoods itself.  Registered as an entry point so
``t3 overlays`` discovers it automatically after ``pip install teatree``.
"""

from teatree.config import default_logging, get_data_dir

_DATA_DIR = get_data_dir("t3-teatree")

SECRET_KEY = "teatree-self-overlay-dev"  # noqa: S105
DEBUG = True
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_htmx",
    "django_rich",
    "django_tasks",
    "django_tasks_db",
    "teatree.core",
    "teatree.agents",
    "teatree.contrib.t3_teatree",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
]

ROOT_URLCONF = "teatree.core.urls"

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

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(_DATA_DIR / "db.sqlite3"),
    },
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGGING = default_logging("t3-teatree")

TEATREE_OVERLAY_CLASS = "teatree.contrib.t3_teatree.overlay.TeatreeOverlay"
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

TASKS = {
    "default": {
        "BACKEND": "django_tasks_db.DatabaseBackend",
    },
}
