"""The allowed destinations for linked worktrees of a managed clone."""

import json
import os
import sqlite3
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path

from teatree.config import cold_db
from teatree.config.loader import clone_root
from teatree.utils.disposable_checkout import disposable_roots


@dataclass(frozen=True, slots=True)
class CheckoutRoots:
    durable: tuple[Path, ...]
    disposable: tuple[Path, ...]
    harness: tuple[Path, ...]
    dropped: tuple[str, ...] = ()
    read_error: str = ""

    def allows(self, path: Path, *, durable: bool = False) -> bool:
        target = path.expanduser().resolve()
        roots = self.durable if durable else (*self.durable, *self.disposable, *self.harness)
        return any(target == root or target.is_relative_to(root) for root in roots)

    def refusal(self, path: Path, *, durable: bool = False) -> str:
        if self.allows(path, durable=durable):
            return ""
        roots = self.durable if durable else (*self.durable, *self.disposable, *self.harness)
        allowed = ", ".join(str(root) for root in roots)
        dropped = f"; ignored unsafe workspace_dir: {', '.join(self.dropped)}" if self.dropped else ""
        degraded = f"; settings store unreadable ({self.read_error}); using worktree base" if self.read_error else ""
        advice = "Use a path under the worktree base or set workspace_dir to a safe durable root."
        if not durable:
            advice = (
                "Use a path under the worktree base, set workspace_dir to a safe durable root, "
                "or use a temp root for a disposable review checkout."
            )
        return f"{path} is outside allowed worktree roots ({allowed}){dropped}{degraded}. {advice}"


def _stored_workspace_dirs() -> tuple[tuple[str, ...], str]:
    try:
        django_conf = import_module("django.conf")
        django_db = import_module("django.db")
    except ImportError:
        django_conf = None
        django_db = None
    if django_conf is not None and django_db is not None and django_conf.settings.configured:
        django_apps = import_module("django.apps")
        django_exceptions = import_module("django.core.exceptions")
        try:
            if django_apps.apps.ready:
                model = django_apps.apps.get_model("core", "ConfigSetting")
                alias = django_db.router.db_for_read(model)
                values = model.objects.using(alias).filter(key="workspace_dir").values_list("value", flat=True)
                return tuple(value for value in values if isinstance(value, str)), ""
        except (
            django_db.DatabaseError,
            django_exceptions.AppRegistryNotReady,
            RuntimeError,
            ValueError,
            TypeError,
            LookupError,
        ):
            pass
    db = cold_db.canonical_config_db()
    if db.exists():
        try:
            rows = cold_db.read_readonly(
                db,
                lambda conn: conn.execute(
                    "SELECT value FROM teatree_config_setting WHERE key=?", ("workspace_dir",)
                ).fetchall(),
            )
            values = tuple(value for (raw,) in rows if isinstance((value := json.loads(raw)), str))
        except (sqlite3.Error, ValueError, TypeError) as exc:
            error = str(exc)
        else:
            return values, ""
    else:
        error = f"{db} unavailable"
    projection = cold_db.canonical_projection()
    if projection is not None:
        values = tuple(
            value
            for settings in projection.settings.values()
            if isinstance((value := settings.get("workspace_dir")), str)
        )
        return values, ""
    return (), error


def checkout_roots(
    *,
    source_clone: Path | None = None,
    home: Path | None = None,
    clone_dir: Path | None = None,
    stored: tuple[str, ...] | None = None,
    disposable: tuple[Path, ...] | None = None,
) -> CheckoutRoots:
    home_root = (home or Path.home()).expanduser().resolve()
    if clone_dir is None:
        try:
            clone_dir = clone_root()
        except ModuleNotFoundError as exc:
            if exc.name is None or not exc.name.startswith("django"):
                raise
            clone_dir = Path(os.environ.get("T3_WORKSPACE_DIR") or str(home_root / "workspace"))
    clone = clone_dir.expanduser().resolve()
    temporary = tuple(root.resolve() for root in (disposable if disposable is not None else disposable_roots()))
    base = (home_root / "workspace" / "t3-workspaces").resolve()
    read_error = ""
    if stored is None:
        stored, read_error = _stored_workspace_dirs()
    durable_roots: dict[Path, None] = {base: None}
    dropped: list[str] = []
    for raw in stored:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            dropped.append(f"{raw} (not absolute)")
            continue
        resolved = candidate.resolve()
        if (
            home_root.is_relative_to(resolved)
            or clone.is_relative_to(resolved)
            or any(resolved.is_relative_to(root) for root in temporary)
        ):
            dropped.append(raw)
            continue
        durable_roots[resolved] = None
    harness = ((source_clone / ".claude" / "worktrees").resolve(),) if source_clone is not None else ()
    return CheckoutRoots(tuple(durable_roots), temporary, harness, tuple(dropped), read_error)


__all__ = ["CheckoutRoots", "checkout_roots"]
