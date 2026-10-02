"""Template-clone the app DB into the test DB, re-cloning only on migration drift.

souliane/teatree#3326. Core already template-clones a Postgres database in
seconds for the *application* DB (``importer._copy_ref_to_ticket`` — drop, then
``createdb -T``); nothing applied it to the *test* DB, so any overlay with a
provisioned app DB and a non-trivial migration history paid a full ``--create-db``
migration replay (minutes) on every test-DB rebuild.

This module is the missing in-between: reuse the existing clone primitives to
template-copy the app DB onto the test DB, and gate the copy on ``django_migrations``
drift — equal → reuse the test DB (``--reuse-db``), different → re-clone (a cheap
``createdb -T``, never a replay).

**Fail toward re-cloning.** The drift check must never mistake an unreadable state
for "in sync": a missing test DB, a connection error, or an unexpected
``django_migrations`` shape all read as *drift*, so uncertainty re-clones rather
than reusing a possibly-stale schema. Opt-in and off by default — no caller that
does not invoke :func:`prepare_test_db` changes behaviour.
"""

import enum
import sys
from typing import ClassVar, TextIO

from teatree.utils.db import pg_env, pg_host, pg_user
from teatree.utils.django_db.helpers import _terminate_connections
from teatree.utils.run import run_allowed_to_fail


class TestDbCloneResult(enum.Enum):
    """Outcome of :func:`prepare_test_db`, mapped to the pytest DB flag by the caller."""

    # A dunder, so enum treats it as a plain attribute (not a member): it tells pytest's
    # --doctest-modules collector not to mistake this ``Test``-prefixed class for a suite.
    __test__: ClassVar[bool] = False

    REUSED = "reused"  # django_migrations matched — the existing test DB is current
    RECLONED = "recloned"  # drift (or uncertainty) — re-cloned from the app DB in seconds
    FAILED = "failed"  # a re-clone was needed but could not run — fall back to a migration replay

    @property
    def is_current(self) -> bool:
        """Whether the test DB is now schema-current — i.e. pytest should ``--reuse-db``.

        ``FAILED`` is the only non-current outcome: the caller keeps ``--create-db``
        so pytest-django replays migrations rather than trusting a stale DB.
        """
        return self is not TestDbCloneResult.FAILED


def _migration_signature(
    db_name: str, *, host: str, user: str, env: dict[str, str]
) -> frozenset[tuple[str, str]] | None:
    """The ``(app, name)`` set from *db_name*'s ``django_migrations``, or ``None`` if unreadable.

    ``None`` is the uncertainty signal (missing DB, connection error, absent table,
    unexpected row shape) — callers treat it as drift and re-clone, never as a match.
    """
    result = run_allowed_to_fail(
        [
            "psql",
            "-h",
            host,
            "-U",
            user,
            "-d",
            db_name,
            "-tAqc",
            "SELECT app, name FROM django_migrations",
        ],
        env=env,
        expected_codes=None,
    )
    if result.returncode != 0:
        return None
    applied: set[tuple[str, str]] = set()
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        app, sep, name = stripped.partition("|")
        if not sep:
            return None
        applied.add((app.strip(), name.strip()))
    return frozenset(applied)


def migrations_drifted(app_db: str, test_db: str, *, host: str, user: str, env: dict[str, str]) -> bool:
    """Whether *test_db*'s applied migrations differ from *app_db*'s.

    Returns ``True`` (drift → re-clone) whenever either database's
    ``django_migrations`` cannot be read as a clean set — the fail-toward-re-clone
    bias. Only two clean, equal signatures return ``False`` (reuse the test DB).
    """
    app_signature = _migration_signature(app_db, host=host, user=user, env=env)
    test_signature = _migration_signature(test_db, host=host, user=user, env=env)
    if app_signature is None or test_signature is None:
        return True
    return app_signature != test_signature


def clone_app_db_to_test_db(*, app_db: str, test_db: str, host: str, user: str, env: dict[str, str]) -> bool:
    """Template-copy *app_db* onto *test_db* in seconds. Returns whether the copy succeeded.

    Mirrors ``importer._copy_ref_to_ticket``: ``dropdb --force`` (PG 13+) atomically
    terminates connections to the old test DB before dropping — otherwise a
    reconnecting worker races ``createdb`` into "database already exists" — then
    ``createdb -T`` template-copies the app DB. Connections to the *template* are
    terminated too: Postgres refuses ``-T`` while the source has active sessions.
    """
    drop = run_allowed_to_fail(
        ["dropdb", "-h", host, "-U", user, "--if-exists", "--force", test_db],
        env=env,
        expected_codes=None,
    )
    if drop.returncode != 0:
        return False
    _terminate_connections(app_db, host, user, env)
    created = run_allowed_to_fail(
        ["createdb", "-h", host, "-U", user, test_db, "-T", app_db],
        env=env,
        expected_codes=None,
    )
    return created.returncode == 0


def prepare_test_db(*, app_db: str, test_db: str, stdout: TextIO | None = None) -> TestDbCloneResult:
    """Bring *test_db* up to date from *app_db* the fast way, returning the outcome.

    The Postgres connection is resolved from the environment (``pg_host`` /
    ``pg_user`` / ``pg_env``), mirroring the importer. No drift → reuse the test DB
    untouched (:attr:`TestDbCloneResult.REUSED`). Drift or uncertainty →
    template-copy the app DB onto it (:attr:`~TestDbCloneResult.RECLONED`), which is
    a ``createdb -T``, not a migration replay. If that copy cannot run, report
    :attr:`~TestDbCloneResult.FAILED` so the caller falls back to ``--create-db``.
    """
    out = stdout if stdout is not None else sys.stdout
    host, user, env = pg_host(), pg_user(), pg_env()

    if not migrations_drifted(app_db, test_db, host=host, user=user, env=env):
        out.write(f"  Test DB {test_db} matches {app_db} (django_migrations in sync) — reusing.\n")
        return TestDbCloneResult.REUSED

    if clone_app_db_to_test_db(app_db=app_db, test_db=test_db, host=host, user=user, env=env):
        out.write(f"  Re-cloned {test_db} from {app_db} (migration drift) via createdb -T — seconds, not a replay.\n")
        return TestDbCloneResult.RECLONED

    out.write(f"  WARNING: Could not clone {app_db} -> {test_db}; falling back to a migration replay.\n")
    return TestDbCloneResult.FAILED
