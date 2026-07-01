"""Default Django settings for teatree.

Used when teatree is the Django project (the standard case).
Auto-discovers overlay Django apps via entry points and adds them to INSTALLED_APPS.
"""

from teatree.config import default_logging
from teatree.paths import CANONICAL_DB, DATA_DIR, DATA_DIR_AUTO_ISOLATED, seed_isolated_db
from teatree.timeouts import CORE_DEFAULTS

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
    "django_linear_migrations",
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
    # Local-only: auto-login the admin dashboard as the superuser when DEBUG is
    # on, so the single-user 127.0.0.1 admin needs no password. Inert off-DEBUG.
    "teatree.core.middleware.LocalAdminAutoLoginMiddleware",
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
# Sourced from the canonical CORE_DEFAULTS registry in teatree.timeouts so the
# two surfaces cannot drift; tests/test_timeouts.py::TestTimeoutRegistryParity
# pins the binding. Override per-overlay via OverlayBase.get_timeouts() or
# per-user via [teatree.timeouts] in ~/.teatree.toml.
TEATREE_TIMEOUTS = dict(CORE_DEFAULTS)
TEATREE_CLAUDE_STATUSLINE_STATE_DIR = "/tmp/claude-statusline"  # noqa: S108 — fixed agent-controlled path, not user input

TASKS = {
    "default": {
        "BACKEND": "django_tasks_db.DatabaseBackend",
        # "default" for every existing task; "loop-runner" is the dedicated queue
        # the #2876 loop-runner beat enqueues per-loop ticks onto (drained in
        # isolation so a fast tick never blocks behind a heavy default-queue job).
        # The literal mirrors teatree.core.tasks.LOOP_RUNNER_QUEUE (parity-tested).
        "QUEUES": ["default", "loop-runner"],
    },
}

# Whether a loop-dispatched phase task runs in-session or headless is the
# ``agent_runtime`` user setting (config/enums.py ``AgentRuntime``), resolved by
# ``core.headless_dispatch.runs_in_session`` — there is no separate Django
# kill-switch. ``interactive`` (default) keeps phase work in the in-session
# ``/loop`` slot; ``sdk_oauth`` / ``sdk_apikey`` / ``api`` run it headless via
# ``agents/headless.py``.

# Repair-loop per-phase iteration budget (#2009). A ticket-phase may re-queue at
# most this many attempts before the re-queue chokepoint
# (``reclaim_orphaned_claims``) refuses with ``MaxIterationsExceeded`` — a
# visible budget replacing the time-only 24h stale-task expiry. Floored at 1.
MAX_PHASE_ITERATIONS = 5
