from pathlib import Path

import teatree

SECRET_KEY = "teatree-tests"
USE_TZ = True
ROOT_URLCONF = "teatree.urls"
STATIC_URL = "/static/"

# The project templates dir (holds the /admin/ re-skin's base_site.html). Mirrors
# ``teatree.settings`` so the admin snapshot renders identically here and in the
# generate-dashboard-snapshot hook.
_PROJECT_TEMPLATES = Path(teatree.__file__).resolve().parent / "templates"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    },
}

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.messages",
    "django.contrib.sessions",
    "django_linear_migrations",
    "django_rich",
    "django_tasks",
    "django_tasks_db",
    "teatree.core",
    "teatree.agents",
    "teatree.backends",
    "teatree.dash",
    "teatree.contrib.t3_teatree",
]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [str(_PROJECT_TEMPLATES)],
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
        "BACKEND": "django_tasks.backends.dummy.DummyBackend",
        # Mirror the production ``teatree.settings`` allowlist: "loops" is the
        # dedicated queue the self-rescheduling loop-timer chains ride (parity-tested).
        "QUEUES": ["default", "loops"],
    },
}

TEATREE_CLAUDE_STATUSLINE_STATE_DIR = "/tmp/claude-statusline"
TEATREE_AGENT_HANDOVER = [
    {
        "runtime": "claude-code",
        "telemetry": {
            "provider": "claude-statusline",
            "switch_away_at_percent": 95,
            "switch_back_at_percent": 80,
        },
    },
]
