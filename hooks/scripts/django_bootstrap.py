"""Bootstrap Django and verify that this hook interpreter can read the ORM database."""

import os
import sys
from pathlib import Path

_MISSING_CAPABILITY_WARNED = False


def _django_setup() -> None:
    import django  # noqa: PLC0415 — deferred cold-hook import

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "teatree.settings")
    django.setup()


def _warn_missing_capability(exc: BaseException) -> None:
    global _MISSING_CAPABILITY_WARNED  # noqa: PLW0603 — process-scoped stderr dedup
    if _MISSING_CAPABILITY_WARNED:
        return
    _MISSING_CAPABILITY_WARNED = True
    sys.stderr.write(
        f"NOTE: the hook interpreter cannot import Django ({type(exc).__name__}: {exc}). "
        "Every ORM-backed gate in this hook process is SKIPPED, not passed — each one "
        "allows its call unvalidated. Point the hooks at an interpreter with teatree's "
        "dependencies installed to restore them.\n"
    )


def _warn_orm_unavailable(status: str, db_path: str, exc: BaseException) -> None:
    global _MISSING_CAPABILITY_WARNED  # noqa: PLW0603 — process-scoped stderr dedup
    if _MISSING_CAPABILITY_WARNED:
        return
    _MISSING_CAPABILITY_WARNED = True
    from teatree.hooks.orm_health import hook_venue  # noqa: PLC0415 — deferred cold-hook import

    sys.stderr.write(
        f"NOTE: hook ORM {status} under {sys.executable} ({hook_venue()}); control DB {db_path}: "
        f"{type(exc).__name__}: {exc}. ORM-backed gates are SKIPPED, not passed.\n"
    )


def _probe_orm() -> tuple[str, str, BaseException | None]:
    from django.db import OperationalError, connections  # noqa: PLC0415 — deferred post-setup import

    from teatree.core.models import StandingGoal  # noqa: PLC0415 — deferred post-setup import

    db_path = str(connections["default"].settings_dict["NAME"])
    if db_path != ":memory:" and not db_path.startswith("file:") and not Path(db_path).is_file():
        return "unreachable", db_path, FileNotFoundError(db_path)
    try:
        StandingGoal.objects.exists()
    except OperationalError as exc:
        status = "unreachable" if "unable to open database file" in str(exc).lower() else "error"
        return status, db_path, exc
    except Exception as exc:  # noqa: BLE001 — an ORM probe failure is not a successful bootstrap
        return "error", db_path, exc
    return "ok", db_path, None


def _record_health(status: str, db_path: str, cause: str) -> None:
    try:
        from teatree.hooks.orm_health import record_orm_health  # noqa: PLC0415 — deferred cold-hook import

        if record_orm_health(status, db_path, cause):
            return
        problem = "the health directory is not writable"
    except Exception as exc:  # noqa: BLE001 — the hook still must allow the call
        problem = f"{type(exc).__name__}: {exc}"
    sys.stderr.write(f"NOTE: hook ORM health marker could not be recorded ({problem}); doctor cannot see this venue.\n")


def bootstrap_teatree_django() -> bool:
    src_dir = Path(__file__).resolve().parents[2] / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))
    try:
        _django_setup()
    except Exception as exc:  # noqa: BLE001 — cold hooks must allow unbootstrappable calls with a warning
        _warn_missing_capability(exc)
        _record_health("error", "unresolved", f"{type(exc).__name__}: {exc}")
        return False
    try:
        status, db_path, cause = _probe_orm()
    except Exception as exc:  # noqa: BLE001 — a failed ORM probe must not become a measured allow
        status, db_path, cause = "error", "unresolved", exc
    if os.environ.get("DJANGO_SETTINGS_MODULE") == "teatree.settings":
        _record_health(status, db_path, "" if cause is None else f"{type(cause).__name__}: {cause}")
    if cause is not None:
        _warn_orm_unavailable(status, db_path, cause)
        return False
    return True
