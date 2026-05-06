"""Default Django settings for teatree.

Used when teatree is the Django project (the standard case).
Auto-discovers overlay Django apps via entry points and adds them to INSTALLED_APPS.
"""

from teatree.config import default_logging, get_data_dir

_DATA_DIR = get_data_dir("teatree")


def _discover_overlay_apps() -> list[str]:
    """Scan ``teatree.overlays`` entry points for overlays that declare a Django app."""
    from importlib.metadata import entry_points  # noqa: PLC0415

    apps: list[str] = []
    for ep in entry_points(group="teatree.overlays"):
        try:
            obj = ep.load()
            app_label = getattr(obj, "django_app", None)
            if app_label:
                apps.append(app_label)
        except Exception:  # noqa: BLE001, S112
            continue
    return apps


SECRET_KEY = "teatree-dev-insecure"  # noqa: S105
DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]
INTERNAL_IPS = ["127.0.0.1"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_rich",
    "django_tasks",
    "django_tasks_db",
    "teatree.core",
    "teatree.agents",
    *_discover_overlay_apps(),
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "teatree.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
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

LOGGING = default_logging("teatree")

# Operation timeouts (seconds).  0 = no timeout.
# Override per-overlay via OverlayBase.get_timeouts() or per-user
# via [teatree.timeouts] in ~/.teatree.toml.
TEATREE_TIMEOUTS = {
    "setup": 120,
    "start": 60,
    "db_import": 180,
    "docker_compose_up": 60,
    "docker_compose_down": 30,
    "provision_step": 120,
    "pre_run_step": 60,
}
TEATREE_CLAUDE_STATUSLINE_STATE_DIR = "/tmp/claude-statusline"  # noqa: S108

TASKS = {
    "default": {
        "BACKEND": "django_tasks_db.DatabaseBackend",
    },
}
