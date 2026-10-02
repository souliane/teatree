"""Safe reconcile of a renumbered migration record (souliane/teatree#1038).

A DSLR snapshot DB carrying an OLD migration numbering fails Django's
``check_consistent_history`` BEFORE any forward migrate runs once master has
RENUMBERED migrations — a migration inserted earlier bumps later numbers, so a
record the snapshot wrote under the old number now looks "applied before its
dependency". The import engine in the sibling ``importer`` module delegates
the recovery here.

This module owns the whole reconcile concern, separated from the import engine
so neither file grows past its module-health budget:

- ``extract_inconsistent_history`` — conformance parser for Django's InconsistentMigrationHistory message.
- ``RECONCILE_SCRIPT`` — the self-contained guarded Django script (run inside the repo's interpreter).
- ``reconcile_renumbered_migration`` — orchestration: parse, run the script via the caller's runner, classify.

The reconcile is satisfiable, not pure suppression: a provable pure renumber
is reconciled and the migrate retried; a genuine divergence is left untouched
so it surfaces as a failure — real schema drift is never masked.
"""

import re
from collections.abc import Callable
from subprocess import CompletedProcess
from typing import TextIO

#: Result sentinels the reconcile script prints to stdout, parsed by the engine.
RECONCILE_OK = "T3_RECONCILE_OK"
RECONCILE_SKIP = "T3_RECONCILE_SKIP"

#: A Python module or a settings module the interpreter could not import.
_MISSING_PYTHON_MARKERS = ("ModuleNotFoundError", "ImproperlyConfigured", "DJANGO_SETTINGS_MODULE", "No module named")

#: A native shared library the loader could not open, in the four wordings the
#: ecosystem emits: glibc's ``ld.so``, ``ctypes.util.find_library`` consumers
#: (python-magic verbatim), ``cffi``/``ctypes.CDLL``, and macOS ``dyld``. A bare
#: ``ImportError`` is deliberately absent — a circular import raises one too, and
#: that is app code, not a package the image is missing.
_MISSING_NATIVE_LIBRARY_MARKERS = (
    "cannot open shared object file",
    "failed to find lib",
    "cannot load library",
    "Library not loaded:",
)

_CONFIG_ERROR_MARKERS = _MISSING_PYTHON_MARKERS + _MISSING_NATIVE_LIBRARY_MARKERS

#: A program name ``PATH`` could not resolve — the exec form of ``FileNotFoundError``
#: (``patchy`` shelling out to ``patch`` at Django startup is the case that cost two
#: provisions). The quoted target must be a bare name: a separator or a suffix means
#: the APP named a data file, which is its own bug rather than a missing package.
_MISSING_PROGRAM_RE = re.compile(r"No such file or directory: '[^'/.]+'")


def is_config_error(combined: str) -> bool:
    """True iff *combined* migrate output blames the ENVIRONMENT, not the app's code.

    The interpreter could not import the app's Python dependencies or settings,
    could not load a native library, or could not resolve a program name — every
    one of them an artifact the IMAGE owns. That is the unverified-venv signature
    gating the dockerized fallback, the unfakeable-migration skip, and (here) the
    refusal to reconcile.

    Kept narrow deliberately: all three consumers read a True as "do not trust
    this interpreter", so an app error misread as one is masked rather than
    reported.
    """
    return any(m in combined for m in _CONFIG_ERROR_MARKERS) or bool(_MISSING_PROGRAM_RE.search(combined))


#: Django's ``check_consistent_history`` raises ``InconsistentMigrationHistory``
#: with exactly this sentence (verbatim from
#: ``django/db/migrations/loader.py::check_consistent_history``). It fires
#: BEFORE any forward migrate runs, so the renumber reconcile must key on this
#: message — not on an ``Applying …`` line, which never appears. The two
#: ``app.name`` captures are: (1) the migration recorded as applied, and
#: (2) the unapplied dependency master now numbers differently.
_INCONSISTENT_HISTORY_RE = re.compile(
    r"Migration (\w+)\.(\w+) is applied before its dependency (\w+)\.(\w+) on database",
)


def extract_inconsistent_history(combined: str) -> tuple[tuple[str, str], tuple[str, str]] | None:
    """Parse Django's ``InconsistentMigrationHistory`` message.

    Returns ``((applied_app, applied_name), (dep_app, dep_name))`` — the
    applied migration and the unapplied dependency named in the error — or
    ``None`` when the output is not that error. The shape mirrors
    ``django/db/migrations/loader.py::check_consistent_history`` verbatim, so a
    Django wording change is caught by the parser test rather than silently
    misclassified as a non-fakeable error (souliane/teatree#1038).
    """
    match = _INCONSISTENT_HISTORY_RE.search(combined)
    if not match:
        return None
    applied_app, applied_name, dep_app, dep_name = match.groups()
    return (applied_app, applied_name), (dep_app, dep_name)


#: Self-contained Django script that SAFELY reconciles a renumbered migration
#: record so ``check_consistent_history`` passes, then lets the forward migrate
#: proceed (souliane/teatree#1038).
#:
#: It runs inside the repo's interpreter via ``manage.py shell -c`` (Django
#: already bootstrapped), reading the dependency named in the
#: ``InconsistentMigrationHistory`` error from env vars (no shell quoting).
#:
#: HARD GUARD — a stale applied record is renamed to the new on-disk dependency
#: ONLY when the conflict is provably a pure RENUMBER, never a genuine
#: divergence:
#:
#:  1. The descriptive suffix (the name after the leading ``NNNN_``) is
#:     BYTE-IDENTICAL between the stale record and the on-disk dependency.
#:     A renumber preserves the suffix; a different migration has a different
#:     suffix. The old file is gone from disk (master renamed it), so the
#:     suffix is the only content signal that survives — and it is the exact
#:     thing Django's own ``makemigrations`` keeps stable across a renumber.
#:  2. Exactly ONE stale applied record carries that suffix (no ambiguity).
#:  3. The stale leading number is strictly LOWER than the dependency's
#:     (an insert only ever bumps numbers upward).
#:  4. The on-disk dependency is currently UNAPPLIED (the renumber's symptom:
#:     the new number looks unapplied while the old number is recorded applied).
#:  5. The dependency's OWN on-disk operations are re-derivable (the file
#:     imports cleanly), proving it is a real migration not a half-written
#:     artifact.
#:
#: When any guard fails it prints ``T3_RECONCILE_SKIP <reason>`` and touches
#: nothing, so real schema drift is surfaced (FAILED), never masked.
RECONCILE_SCRIPT = r"""
import os
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
from django.db.migrations.writer import OperationWriter

OK = "T3_RECONCILE_OK"
SKIP = "T3_RECONCILE_SKIP"


def _suffix(name):
    head, _sep, rest = name.partition("_")
    return rest if head.isdigit() and rest else name


def _leading_number(name):
    head = name.split("_", 1)[0]
    return int(head) if head.isdigit() else None


dep_app = os.environ["T3_RECONCILE_DEP_APP"]
dep_name = os.environ["T3_RECONCILE_DEP_NAME"]

loader = MigrationLoader(connection, ignore_no_migrations=True)
disk = loader.disk_migrations.get((dep_app, dep_name))
if disk is None:
    print(SKIP + " dependency-not-on-disk")
    raise SystemExit(0)

# Guard 5: the dependency's own content must be re-derivable from disk.
try:
    _ = tuple(OperationWriter(op, indentation=0).serialize()[0] for op in disk.operations)
except Exception as exc:  # noqa: BLE001 — an unreadable dependency skips the reconcile cleanly
    print(SKIP + " dependency-unreadable %r" % (exc,))
    raise SystemExit(0)

recorder = MigrationRecorder(connection)
applied = recorder.applied_migrations()

# Guard 4: the renumbered dependency must currently look unapplied.
if (dep_app, dep_name) in applied:
    print(SKIP + " dependency-already-applied")
    raise SystemExit(0)

on_disk_names = {n for (a, n) in loader.disk_migrations if a == dep_app}
# Stale applied records: recorded as applied for this app but absent from disk
# — the renumber's leftover (the OLD number whose file master renamed away).
stale = [n for (a, n) in applied if a == dep_app and n not in on_disk_names]

dep_suffix = _suffix(dep_name)
dep_num = _leading_number(dep_name)

# Guard 1 + 3: keep only stale records whose descriptive suffix is identical
# AND whose leading number is strictly lower than the dependency's.
candidates = [
    n
    for n in stale
    if _suffix(n) == dep_suffix
    and _leading_number(n) is not None
    and dep_num is not None
    and _leading_number(n) < dep_num
]

# Guard 2: exactly one unambiguous candidate.
if len(candidates) != 1:
    print(SKIP + " no-unique-renumber-candidate count=%d suffix=%s" % (len(candidates), dep_suffix))
    raise SystemExit(0)

stale_name = candidates[0]
with connection.cursor() as cur:
    cur.execute(
        "UPDATE django_migrations SET name = %s WHERE app = %s AND name = %s",
        [dep_name, dep_app, stale_name],
    )
print(OK + " %s.%s -> %s.%s" % (dep_app, stale_name, dep_app, dep_name))
"""

#: Runs one ``manage.py`` invocation in the repo's interpreter and returns the
#: completed process. The import engine supplies its own runner (host or
#: dockerized — #1977) so the reconcile reuses the exact same interpreter the
#: migrate ran in; a config/import failure there is the engine's to classify.
ManagepyRunner = Callable[[list[str], dict[str, str]], CompletedProcess[str]]


def reconcile_renumbered_migration(
    combined: str,
    run_env: dict[str, str],
    *,
    run_managepy: ManagepyRunner,
    stdout: TextIO,
) -> bool:
    """Safely reconcile a renumbered migration record on InconsistentMigrationHistory.

    Returns True when a stale ``django_migrations`` record was renamed to
    master's new migration name (so the caller should retry the migrate);
    False when the error is not an inconsistent-history renumber, or the
    conflict is not a *provable* pure renumber (genuine divergence — left
    untouched so it surfaces as FAILED). See :data:`RECONCILE_SCRIPT` for the
    hard guards (souliane/teatree#1038).
    """
    parsed = extract_inconsistent_history(combined)
    if parsed is None:
        return False
    # A config/import error can co-occur; never act on an unverified venv.
    if is_config_error(combined):
        return False
    (applied_app, applied_name), (dep_app, dep_name) = parsed
    stdout.write(
        f"  InconsistentMigrationHistory: {applied_app}.{applied_name} applied before "
        f"{dep_app}.{dep_name}; checking for a pure renumber...\n"
    )
    reconcile_env = {**run_env, "T3_RECONCILE_DEP_APP": dep_app, "T3_RECONCILE_DEP_NAME": dep_name}
    result = run_managepy(["manage.py", "shell", "-c", RECONCILE_SCRIPT], reconcile_env)
    out = f"{result.stdout}\n{result.stderr}"
    if result.returncode == 0 and RECONCILE_OK in out:
        renamed = out.split(RECONCILE_OK, 1)[1].strip().splitlines()[0]
        stdout.write(f"  Reconciled renumbered migration record: {renamed}\n")
        return True
    # SKIP (provably-not-a-renumber) or any error: do NOT mask drift.
    reason = ""
    if RECONCILE_SKIP in out:
        reason = out.split(RECONCILE_SKIP, 1)[1].strip().splitlines()[0]
    stdout.write(f"  Not a reconcilable renumber ({reason or 'reconcile failed'}); leaving records untouched.\n")
    return False
