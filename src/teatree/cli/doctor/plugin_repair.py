"""Plugin marketplace + registration repair helpers used by `t3 doctor check`.

Split out of ``teatree.cli.doctor`` (souliane/teatree#1270). These helpers repair
the Claude plugin registration (`known_marketplaces.json`, `installed_plugins.json`,
`enabledPlugins` in `settings.json`), and they WRITE only under ``--repair``: a plain
`t3 doctor check` reports the drift and touches nothing, as ``--repair``'s own help
text promises. Kept private — re-exported from ``teatree.cli.doctor`` for backward
compatibility with existing test imports.
"""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import typer

_CLAUDE_PLUGIN_ID = "t3@souliane"


class UnparsableJson:
    """Sentinel: the file exists but is not readable JSON, so its content is UNKNOWN.

    Distinct from the empty dict an ABSENT file yields. Collapsing the two let a
    momentarily-unparsable ``~/.claude/settings.json`` (Claude Code mid-write, a
    truncated write, a trailing comma the operator just typed) read as "no keys set",
    so the repair below rewrote the operator's whole configuration — permissions,
    hooks, env, statusLine — as a one-key file.
    """


def _read_json_safe(path: Path) -> "dict | type[UnparsableJson]":
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return UnparsableJson


def _resolve_main_clone() -> Path | None:
    from teatree.cli.doctor import DoctorService  # noqa: PLC0415 — deferred: breaks plugin_repair ↔ doctor cycle

    env_path = os.environ.get("T3_REPO", "")
    if env_path:
        candidate = Path(env_path).expanduser()
        if (candidate / "pyproject.toml").is_file():
            return candidate
    try:
        repo = DoctorService.find_teatree_repo()
    except OSError:
        return None
    if not repo:
        return None
    git = repo / ".git"
    if git.is_file():
        match = re.match(r"^gitdir:\s*(.+)$", git.read_text().strip())
        if match:
            main_git = Path(match.group(1)).parent.parent
            if main_git.name == ".git" and main_git.is_dir():
                return main_git.parent
    return repo


@dataclass(frozen=True, slots=True)
class RegistrationOutcome:
    """What one registration artifact needed, and whether this run wrote it.

    An empty *detail* means the artifact already names the expected clone. A detail
    with ``written=False`` is drift this run deliberately did not touch — either
    because ``--repair`` was not given, or because the file could not be read and
    writing it would replace its whole content.
    """

    detail: str = ""
    written: bool = False


def _unreadable(path: Path) -> RegistrationOutcome:
    return RegistrationOutcome(f"{path} is not readable JSON — refusing to write over it")


def _repair_marketplace_json(plugins_dir: Path, target: str, now: str, *, repair: bool = False) -> RegistrationOutcome:
    """Reconcile the marketplace registration against *target*; write only under *repair*."""
    path = plugins_dir / "known_marketplaces.json"
    data = _read_json_safe(path)
    if not isinstance(data, dict):
        return _unreadable(path)
    mp_name = _CLAUDE_PLUGIN_ID.split("@", 1)[1]
    if data.get(mp_name, {}).get("installLocation") == target:
        return RegistrationOutcome()
    if not repair:
        return RegistrationOutcome(f"{path} does not install {mp_name} from {target}")
    data[mp_name] = {
        "source": {"source": "directory", "path": target},
        "installLocation": target,
        "lastUpdated": now,
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return RegistrationOutcome(f"repaired {path}", written=True)


def _repair_installed_plugins(plugins_dir: Path, target: str, now: str, *, repair: bool = False) -> RegistrationOutcome:
    """Reconcile the installed-plugins entry against *target*; write only under *repair*."""
    path = plugins_dir / "installed_plugins.json"
    data = _read_json_safe(path)
    if not isinstance(data, dict):
        return _unreadable(path)
    plugins = data.setdefault("plugins", {})
    entries = plugins.get(_CLAUDE_PLUGIN_ID, [])
    if entries and entries[0].get("installPath") == target:
        return RegistrationOutcome()
    if not repair:
        return RegistrationOutcome(f"{path} does not install {_CLAUDE_PLUGIN_ID} from {target}")
    data.setdefault("version", 2)
    plugins[_CLAUDE_PLUGIN_ID] = [
        {
            "scope": "user",
            "installPath": target,
            "version": "local",
            "installedAt": entries[0].get("installedAt", now) if entries else now,
            "lastUpdated": now,
        },
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return RegistrationOutcome(f"repaired {path}", written=True)


def _repair_enabled_plugins(*, repair: bool = False) -> RegistrationOutcome:
    """Reconcile ``enabledPlugins`` in the operator's own ``~/.claude/settings.json``.

    The riskiest of the three: that file also carries permissions, hooks, env and the
    statusLine block, and it is the operator's, not teatree's. An unparsable read is
    reported and never written over — a ``{}`` stand-in would have replaced the whole
    file with a single ``enabledPlugins`` key.
    """
    settings_path = Path.home() / ".claude" / "settings.json"
    resolved = settings_path.resolve() if settings_path.is_file() else settings_path
    data = _read_json_safe(resolved)
    if not isinstance(data, dict):
        return _unreadable(resolved)
    enabled = data.setdefault("enabledPlugins", {})
    if enabled.get(_CLAUDE_PLUGIN_ID) is True:
        return RegistrationOutcome()
    if not repair:
        return RegistrationOutcome(f"{resolved} does not enable {_CLAUDE_PLUGIN_ID}")
    enabled[_CLAUDE_PLUGIN_ID] = True
    resolved.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return RegistrationOutcome(f"repaired {resolved}", written=True)


def _ensure_plugin_registered(*, repair: bool = False) -> bool:
    """Verify t3 plugin registration, repairing it only under *repair*.

    Called at every ``t3 doctor check`` (and thus every Claude session start).
    Best-effort — never fails the check if the repo or filesystem is unavailable.
    """
    try:
        return _do_ensure_plugin_registered(repair=repair)
    except OSError:
        return True


def _do_ensure_plugin_registered(*, repair: bool = False) -> bool:
    repo = _resolve_main_clone()
    if not repo:
        return True

    from datetime import UTC, datetime  # noqa: PLC0415 — deferred: loaded only when this command runs

    target = str(repo.resolve())
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    plugins_dir = Path.home() / ".claude" / "plugins"

    outcomes = [
        _repair_marketplace_json(plugins_dir, target, now, repair=repair),
        _repair_installed_plugins(plugins_dir, target, now, repair=repair),
        _repair_enabled_plugins(repair=repair),
    ]
    if any(outcome.written for outcome in outcomes):
        typer.echo(f"OK    Repaired {_CLAUDE_PLUGIN_ID} plugin registration → {target}")
    untouched = [outcome.detail for outcome in outcomes if outcome.detail and not outcome.written]
    for detail in untouched:
        typer.echo(f"WARN  Plugin registration: {detail}")
    if untouched and not repair:
        typer.echo(f"WARN  Plugin registration: `t3 doctor check --repair` points {_CLAUDE_PLUGIN_ID} at {target}")
    return True
