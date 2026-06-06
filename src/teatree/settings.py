"""Default Django settings for teatree.

Used when teatree is the Django project (the standard case).
Auto-discovers overlay Django apps via entry points and adds them to INSTALLED_APPS.
"""

from teatree.config import default_logging
from teatree.paths import CANONICAL_DB, DATA_DIR, DATA_DIR_AUTO_ISOLATED, seed_isolated_db

_DATA_DIR = DATA_DIR
if DATA_DIR_AUTO_ISOLATED:
    seed_isolated_db(_DATA_DIR)
_DATA_DIR.mkdir(parents=True, exist_ok=True)

# The stale-DB notice is an operational nudge surfaced by ``t3 doctor check``
# (_check_single_db), not a Python warning at settings import: every pytest
# collection imports settings under ``filterwarnings=error``, so emitting it
# here turned a benign legacy db.sqlite3 into a hard collection error.


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


SECRET_KEY = "teatree-dev-insecure"  # noqa: S105 — local-dev CLI, never deployed
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
    "teatree.backends",
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

# SQLite write serialization for the production engine.
#
# Django's SQLite backend silently ignores ``select_for_update()`` — it is a
# documented no-op (SQLite has no row-level locks).  The shared-state
# read-modify-write sites (``Session.visit_phase``, ``Task.claim`` and ~12
# siblings) wrap their RMW in ``transaction.atomic()`` + ``select_for_update()``
# expecting mutual exclusion.  Without connection-level serialization two
# concurrent workers both ``BEGIN DEFERRED``, both read the row, both mutate
# and both commit — the exact lost-update those locks are written to prevent.
#
# ``transaction_mode="IMMEDIATE"`` (Django 5.1+) makes every ``atomic()`` block
# open with ``BEGIN IMMEDIATE``, so the first writer takes SQLite's reserved
# write lock at transaction start and concurrent writers block instead of
# racing — restoring the invariant the ``select_for_update()`` calls assume.
#
# ``journal_mode=WAL`` lets readers run concurrently with the single writer
# (avoids needless reader/writer contention) while still serializing writers.
#
# ``timeout`` maps to SQLite's ``busy_timeout``: a blocked writer waits this
# long for the reserved lock before raising ``database is locked`` instead of
# failing immediately.  30s comfortably exceeds the longest single locked RMW
# (the claim / visit_phase ops are sub-second) plus headroom for a backlog of
# contending workers, while still failing loudly rather than hanging forever.
#
# Exposed as a named constant so the concurrency regression test can import
# the exact production value; reverting this to ``{}`` is the single hunk that
# flips that test RED.
SQLITE_WRITE_SERIALIZATION_OPTIONS = {
    "timeout": 30,
    "init_command": "PRAGMA journal_mode=WAL;",
    "transaction_mode": "IMMEDIATE",
}

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(CANONICAL_DB),
        "OPTIONS": SQLITE_WRITE_SERIALIZATION_OPTIONS,
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
    "docker_compose_build": 600,
    "docker_compose_down": 30,
    "provision_step": 120,
    "pre_run_step": 60,
}
TEATREE_CLAUDE_STATUSLINE_STATE_DIR = "/tmp/claude-statusline"  # noqa: S108 — fixed agent-controlled path, not user input

TASKS = {
    "default": {
        "BACKEND": "django_tasks_db.DatabaseBackend",
    },
}

# Single auditable kill-switch for the legacy detached ``claude -p`` phase
# dispatch (``execute_headless_task``). Default OFF: post the 2026-06-15
# billing change a ``claude -p`` run is metered, so loop-dispatched phase
# tasks run INTERACTIVE (subscription-covered) via the in-session ``/loop``
# slot. When OFF, ``execute_headless_task`` refuses to shell ``claude -p``
# for any ``(role, phase)`` with a registered phase agent and records a
# ``routing_error`` instead. Flip to ``True`` only to deliberately re-enable
# metered headless dispatch for loop-dispatched phases.
LOOP_ALLOW_HEADLESS_DISPATCH = False
