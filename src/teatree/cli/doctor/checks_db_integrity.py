"""SQLite integrity, isolation and projection probes for `t3 doctor check`.

The INTEGRITY probe is the one doctor check that must WRITE-test. Every other probe
reads, and a corrupt SQLite b-tree reads fine on almost every table while writes to any
AUTOINCREMENT table die with a raw ``DatabaseError`` — so a read-only doctor reports
green on a database the factory can no longer record work in. That is the exact shape of
the corruption this install already took: reads served 111 of 112 tables and nothing
surfaced it until a random write threw. A database failing ``PRAGMA quick_check`` is not
degraded, it is unusable for writes, so this is a HARD FAIL, not an advisory WARN. It is
cheap: ``quick_check`` skips ``integrity_check``'s full index cross-reference, so it is
O(pages) rather than O(index entries) and returns well under a second here.

The ISOLATION and WRITER probes are the cause side, and they assert facts that are true
NOW: the database is in its named volume where no host process can name it, and no
process outside the owning domain holds it read-write. Their predecessor asserted that a
claim FILE existed beside the database, and reported green throughout every corruption —
a marker written once says nothing about the descriptors that were already open.

The PROJECTION probe closes the loop the volume opens. Host hooks can no longer read the
database, so they read a strictly-derived projection of it; only the owning side can
compare that projection's generation against the source, which makes this the one place
a publisher that has stopped becomes visible instead of silently serving old values.

All four run every time. Repairing pages a second writer will cross-link again is not a
fix, so no probe short-circuits the others.
"""

import sqlite3
from pathlib import Path

import typer
from django.conf import settings
from django.db import DatabaseError, connections

from teatree.config.cold_db import projection_dir_for
from teatree.config.host_projection import ProjectionPublisher, ProjectionReader
from teatree.db.write_domain import ControlDbWriteDomain, FdHolder, read_write_holders_across
from teatree.paths import DATA_DIR, TRUE_CANONICAL_DB, find_control_db_artifacts

# quick_check returns exactly this single row when the database is sound.
_SQLITE_OK = "ok"

# quick_check reports one row per problem and a wide corruption can produce thousands.
# The operator needs the shape of the failure, not a wall of page numbers.
_MAX_REPORTED_PROBLEMS = 3


def _sqlite_path() -> Path | None:
    """The default connection's on-disk file, or ``None`` when it is not file-backed SQLite."""
    config = settings.DATABASES.get("default", {})
    if "sqlite" not in config.get("ENGINE", ""):
        return None
    name = str(config.get("NAME", ""))
    # ``:memory:`` and the empty name are not files; a test run must never fail this check.
    if not name or name == ":memory:":
        return None
    return Path(name)


def _quick_check() -> list[str]:
    """Run ``PRAGMA quick_check`` on the default connection; return problems (empty when sound).

    Deliberately Django's own connection rather than a second ``sqlite3.connect``. A raw
    handle opened alongside Django's is a second lifecycle to get right in every context
    this check runs in — including the test suite, where finalising it during garbage
    collection surfaces as an unraisable exception inside whatever unrelated test happens
    to be running when the collector fires. Django already owns a connection to exactly the
    file we want to interrogate; borrowing it removes the whole class of problem.

    A database too damaged to answer the PRAGMA raises instead of returning rows, and that
    IS the finding — so the error is reported, never propagated.
    """
    try:
        with connections["default"].cursor() as cursor:
            cursor.execute("PRAGMA quick_check")
            rows = [str(row[0]) for row in cursor.fetchall()]
    except DatabaseError as corrupt:
        return [str(corrupt)]
    return [] if rows == [_SQLITE_OK] else rows


def _check_db_integrity() -> bool:
    path = _sqlite_path()
    if path is None or not path.exists():
        return True

    problems = _quick_check()
    if not problems:
        typer.secho(f"OK    SQLite integrity: quick_check clean ({path.name})", fg=typer.colors.GREEN)
        return True

    shown = "; ".join(problems[:_MAX_REPORTED_PROBLEMS])
    hidden = len(problems) - _MAX_REPORTED_PROBLEMS
    suffix = f" (+{hidden} more)" if hidden > 0 else ""
    typer.secho(
        f"FAIL  SQLite integrity: {path} FAILS PRAGMA quick_check — {shown}{suffix}. "
        "Reads still serve most tables, so this stays invisible until a write to an "
        "AUTOINCREMENT table throws DatabaseError. Recover with: "
        f"sqlite3 {path} '.recover' | sqlite3 {path}.recovered — verify quick_check on the "
        "result, compare row counts, then swap it in with the stale -wal/-shm REMOVED "
        "(replaying them reintroduces the bad pages). "
        "Concurrent writers across a VM boundary in WAL mode are the usual cause.",
        fg=typer.colors.RED,
    )
    return False


def _check_db_is_off_the_host_filesystem() -> bool:
    """The control DB must live in its named volume, where no host process can name it.

    Asserted against the CANONICAL path rather than the live connection: a worktree
    checkout is deliberately isolated onto its own database, and reporting on that copy
    would answer a question nobody asked. An isolated database has no volume to be in,
    so the check simply does not apply there.
    """
    domain = ControlDbWriteDomain(TRUE_CANONICAL_DB)
    if not domain.on_host_filesystem:
        typer.secho(
            f"OK    Control DB filesystem: {TRUE_CANONICAL_DB.parent} is the control-DB volume, not a host bind mount",
            fg=typer.colors.GREEN,
        )
        return True

    typer.secho(
        f"FAIL  Control DB filesystem: {TRUE_CANONICAL_DB} is on a host-reachable filesystem "
        f"(expected the volume at {domain.expected_dir}). A database the host can name is a "
        "database host processes hold descriptors on, and a descriptor opened before the "
        "container claimed ownership is never revoked. Mount the teatree_control_db volume "
        "and run deploy/migrate-control-db-to-volume.sh.",
        fg=typer.colors.RED,
    )
    return False


def _control_db_writers() -> list[tuple[Path, FdHolder]]:
    """Every (database, writer) pair that must not exist — across BOTH reachable databases.

    Two databases, two questions, because they are reachable differently.

    The CANONICAL one lives in a volume with no host path, so only a writer from
    OUTSIDE the owning domain is a fault: inside the container a read-write
    descriptor is the stack doing its job. On the host that file does not exist at
    all, which is precisely why asking about it alone leaves the probe INERT exactly
    where the damage happens.

    A COPY left under the host data dir has no legitimate writer on either side.
    Nothing should be holding a superseded control database open, so this half asks
    for every holder rather than only foreign ones — `foreign_writers` would answer
    nothing whenever the doctor runs where `t3` is mandated to run, since a
    container cannot see the host's process table.
    """
    canonical = (
        [(TRUE_CANONICAL_DB, writer) for writer in ControlDbWriteDomain(TRUE_CANONICAL_DB).foreign_writers()]
        if TRUE_CANONICAL_DB.exists()
        else []
    )
    return canonical + read_write_holders_across(list(find_control_db_artifacts(DATA_DIR, canonical=TRUE_CANONICAL_DB)))


def _check_no_host_process_holds_the_db_writable() -> bool:
    """FAIL naming every process holding the control DB — or a host copy of it — read-write.

    This is the condition that did the damage, observed directly rather than inferred
    from a marker file: the claim-file check this replaces reported green throughout
    every corruption, because a claim says a container once wrote a marker and says
    nothing about the descriptors already open when it did.
    """
    offenders = _control_db_writers()
    if not offenders:
        typer.secho(
            "OK    Control DB writers: no process holds the control DB or a host copy of it read-write",
            fg=typer.colors.GREEN,
        )
        return True

    held = "; ".join(f"{database} — {writer}" for database, writer in offenders)
    typer.secho(
        f"FAIL  Control DB writers: {len(offenders)} descriptor(s) hold the control DB read-write — {held}. "
        "Write access is granted once at connection setup and never revoked, so each of these keeps "
        "writing for its whole life, and RENAMING the file retires the name rather than the descriptor. "
        "Stop the processes, remove the host copies so no descriptor can be re-acquired, and route the "
        "work through the container (deploy/t3 <args>).",
        fg=typer.colors.RED,
    )
    return False


def _check_host_projection_is_current() -> bool:
    """The projection the Django-free hooks read must match the generation in the source.

    Only the owning side can compare the two — the host cannot open the source at all —
    so this is where a publisher that has silently stopped becomes visible. Without it,
    a stale projection reads exactly like a fresh one and the hooks quietly serve old
    kill-switch values, which is #3499 all over again.

    The source generation is read from the database FILE rather than through the ORM,
    because the file is what the publisher projects from: comparing against whichever
    connection happens to be open would answer about a different database.

    A source that cannot be read FAILs. The faults that make it unreadable — a long
    writer holding the lock, a WAL recovery in progress — are the same ones that stop
    the publisher, so returning quietly reported the projection as current on exactly
    the failure this check exists to catch, and emitted no line for the JSON finding
    parser to see either. An unreadable source is not evidence of a current projection.
    """
    path = _sqlite_path()
    if path is None or not path.exists():
        return True

    published = ProjectionReader(projection_dir_for(path)).read().projection
    try:
        expected = ProjectionPublisher(path, projection_dir_for(path)).build().generation
    except sqlite3.Error as exc:
        typer.secho(
            f"FAIL  Host projection: could not read the source generation from {path} ({exc}). "
            "Whether the projection is current is unknown, so every Claude Code hook reading a "
            "DB-home setting without Django may be serving values this generation did not produce.",
            fg=typer.colors.RED,
        )
        return False
    if published is not None and published.generation == expected:
        typer.secho(
            f"OK    Host projection: current at generation {expected}",
            fg=typer.colors.GREEN,
        )
        return True

    found = published.generation if published is not None else "nothing published"
    typer.secho(
        f"FAIL  Host projection: the source is at generation {expected} but the projection in "
        f"{projection_dir_for(path)} is at {found}. Every Claude Code hook that reads a DB-home "
        "setting without Django is serving values this generation did not produce. The publisher "
        "runs on the config write seam (teatree.config.host_projection); check the worker log for "
        "a failed publication.",
        fg=typer.colors.RED,
    )
    return False


def _check_database_health() -> bool:
    """The database gate: corruption, a reachable database, a foreign writer, a stale projection.

    All four always run. The filesystem and writer results are the CAUSE the integrity
    result is the symptom of, so reporting only the first failure found leaves the
    operator repairing pages that a second writer will cross-link again.
    """
    sound = _check_db_integrity()
    isolated = _check_db_is_off_the_host_filesystem()
    uncontended = _check_no_host_process_holds_the_db_writable()
    projected = _check_host_projection_is_current()
    return sound and isolated and uncontended and projected
