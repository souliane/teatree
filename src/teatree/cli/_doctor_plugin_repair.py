"""Plugin marketplace + registration repair helpers used by `t3 doctor check`.

Split out of ``teatree.cli.doctor`` (souliane/teatree#1270). These helpers
auto-repair the Claude plugin registration (`known_marketplaces.json`,
`installed_plugins.json`, `enabledPlugins` in `settings.json`) on every
`t3 doctor check`. Kept private — re-exported from ``teatree.cli.doctor``
for backward compatibility with existing test imports.
"""

import json
import os
import re
from pathlib import Path

import typer

_CLAUDE_PLUGIN_ID = "t3@souliane"


def _read_json_safe(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _resolve_main_clone() -> Path | None:
    from teatree.cli.doctor import DoctorService  # noqa: PLC0415

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


def _repair_marketplace_json(plugins_dir: Path, target: str, now: str) -> bool:
    path = plugins_dir / "known_marketplaces.json"
    data = _read_json_safe(path)
    mp_name = _CLAUDE_PLUGIN_ID.split("@", 1)[1]
    if data.get(mp_name, {}).get("installLocation") == target:
        return False
    data[mp_name] = {
        "source": {"source": "directory", "path": target},
        "installLocation": target,
        "lastUpdated": now,
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def _repair_installed_plugins(plugins_dir: Path, target: str, now: str) -> bool:
    path = plugins_dir / "installed_plugins.json"
    data = _read_json_safe(path)
    plugins = data.setdefault("plugins", {})
    entries = plugins.get(_CLAUDE_PLUGIN_ID, [])
    if entries and entries[0].get("installPath") == target:
        return False
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
    return True


def _repair_enabled_plugins() -> bool:
    settings_path = Path.home() / ".claude" / "settings.json"
    resolved = settings_path.resolve() if settings_path.is_file() else settings_path
    data = _read_json_safe(resolved)
    enabled = data.setdefault("enabledPlugins", {})
    if enabled.get(_CLAUDE_PLUGIN_ID) is True:
        return False
    enabled[_CLAUDE_PLUGIN_ID] = True
    resolved.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def _ensure_plugin_registered() -> bool:
    """Verify and auto-repair t3 plugin registration.

    Called at every ``t3 doctor check`` (and thus every Claude session start).
    Best-effort — never fails the check if the repo or filesystem is unavailable.
    """
    try:
        return _do_ensure_plugin_registered()
    except OSError:
        return True


def _do_ensure_plugin_registered() -> bool:
    repo = _resolve_main_clone()
    if not repo:
        return True

    from datetime import UTC, datetime  # noqa: PLC0415

    target = str(repo.resolve())
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    plugins_dir = Path.home() / ".claude" / "plugins"

    repaired = _repair_marketplace_json(plugins_dir, target, now)
    repaired = _repair_installed_plugins(plugins_dir, target, now) or repaired
    repaired = _repair_enabled_plugins() or repaired

    if repaired:
        typer.echo(f"OK    Auto-repaired {_CLAUDE_PLUGIN_ID} plugin → {target}")
    return True
