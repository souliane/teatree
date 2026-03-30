SECRET_KEY = "teatree-tests"
USE_TZ = True
ROOT_URLCONF = "tests.urls"

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
    "django_htmx",
    "django_rich",
    "django_tasks",
    "django_tasks_db",
    "teatree.core",
    "teatree.agents",
    "teatree.contrib.t3_teatree",
]

TEATREE_OVERLAY_CLASS = "teatree.contrib.t3_teatree.overlay.TeatreeOverlay"

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

TEATREE_HEADLESS_RUNTIME = "claude-code"
TEATREE_INTERACTIVE_RUNTIME = "codex"
TEATREE_TERMINAL_MODE = "same-terminal"
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
    {
        "runtime": "codex",
    },
]
