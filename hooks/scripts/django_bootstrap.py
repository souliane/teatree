"""Bootstrap Django and verify that this hook interpreter can read the ORM database."""

import os
import sys
from pathlib import Path

_EMITTED_NOTES: set[str] = set()

# What importing teatree and its dependencies can raise; Django's own errors are named by orm_failures().
_SETUP_FAILURES = (ImportError, OSError, RuntimeError, SyntaxError)


def orm_failures() -> tuple[type[Exception], ...]:
    from django.core.exceptions import ImproperlyConfigured  # noqa: PLC0415 — deferred cold-hook import
    from django.db import Error  # noqa: PLC0415 — deferred cold-hook import

    return Error, ImproperlyConfigured, ImportError, OSError


def _django_setup() -> None:
    import django  # noqa: PLC0415 — deferred cold-hook import

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "teatree.settings")
    django.setup()


def _note_once(note: str) -> None:
    if _EMITTED_NOTES:
        return
    _EMITTED_NOTES.add(note)
    sys.stderr.write(note)


def _warn_missing_capability(exc: BaseException) -> None:
    _note_once(
        f"NOTE: the hook interpreter cannot import Django ({type(exc).__name__}: {exc}). "
        "Every ORM-backed gate in this hook process is SKIPPED, not passed — each one "
        "allows its call unvalidated. Point the hooks at an interpreter with teatree's "
        "dependencies installed to restore them.\n"
    )


def _warn_orm_unavailable(status: str, db_path: str, exc: BaseException) -> None:
    from teatree.hooks.orm_health import hook_venue  # noqa: PLC0415 — deferred cold-hook import

    _note_once(
        f"NOTE: hook ORM {status} under {sys.executable} ({hook_venue()}); control DB {db_path}: "
        f"{type(exc).__name__}: {exc}. ORM-backed gates are SKIPPED, not passed.\n"
    )


def _probe_orm() -> tuple[str, str, BaseException | None]:
    from django.db import OperationalError, connections  # noqa: PLC0415 — deferred post-setup import

    db_path = "unresolved"
    try:
        from teatree.core.models import StandingGoal  # noqa: PLC0415 — deferred post-setup import

        db_path = str(connections["default"].settings_dict["NAME"])
        if db_path != ":memory:" and not db_path.startswith("file:") and not Path(db_path).is_file():
            return "unreachable", db_path, FileNotFoundError(db_path)
        StandingGoal.objects.exists()
    except OperationalError as exc:
        status = "unreachable" if "unable to open database file" in str(exc).lower() else "error"
        return status, db_path, exc
    except orm_failures() as exc:
        return "error", db_path, exc
    return "ok", db_path, None


def _record_health(status: str, db_path: str, cause: str) -> None:
    try:
        from teatree.hooks.orm_health import record_orm_health  # noqa: PLC0415 — deferred cold-hook import

        if record_orm_health(status, db_path, cause):
            return
        problem = "the health directory is not writable"
    except (ImportError, OSError, RuntimeError) as exc:  # RuntimeError: no home or data dir to resolve
        problem = f"{type(exc).__name__}: {exc}"
    sys.stderr.write(f"NOTE: hook ORM health marker could not be recorded ({problem}); doctor cannot see this venue.\n")


def _setup_failed(exc: BaseException) -> bool:
    _warn_missing_capability(exc)
    _record_health("error", "unresolved", f"{type(exc).__name__}: {exc}")
    return False


def bootstrap_teatree_django() -> bool:
    src_dir = Path(__file__).resolve().parents[2] / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))
    try:
        _django_setup()
    except _SETUP_FAILURES as exc:
        return _setup_failed(exc)
    except orm_failures() as exc:
        return _setup_failed(exc)
    status, db_path, cause = _probe_orm()
    if os.environ.get("DJANGO_SETTINGS_MODULE") == "teatree.settings":
        _record_health(status, db_path, "" if cause is None else f"{type(cause).__name__}: {cause}")
    if cause is not None:
        _warn_orm_unavailable(status, db_path, cause)
        return False
    return True
