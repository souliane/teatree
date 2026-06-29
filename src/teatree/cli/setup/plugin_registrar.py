"""Claude-plugin and marketplace registration for ``t3 setup``."""

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import typer

_PLUGIN_NAME = "t3"
_MARKETPLACE_NAME = "souliane"
_PLUGIN_ID = f"{_PLUGIN_NAME}@{_MARKETPLACE_NAME}"


def _read_json(path: Path) -> dict:
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _settings_path() -> Path:
    """Return the resolved settings.json path (follows symlinks)."""
    path = Path.home() / ".claude" / "settings.json"
    return path.resolve() if path.is_file() else path


class PluginRegistrar:
    """Register the t3 plugin + marketplace pointing at the local main clone."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def install(self) -> bool:
        """Register the t3 plugin pointing directly at the local main clone.

        Uses the same ``installed_plugins.json`` format as marketplace-installed
        plugins so Claude Code treats it identically (namespaced skills, visible
        in ``claude plugin list``).  The ``installPath`` points directly at the
        main clone — no cache copy, always live.
        """
        self._cleanup_legacy()
        self._register_marketplace()
        self.register_installed()
        self.enable()
        typer.echo(f"OK    Plugin {_PLUGIN_ID} registered (installPath: {self.repo.resolve()}).")
        return True

    def register_installed(self) -> None:
        """Register t3 in installed_plugins.json with installPath pointing to the main clone."""
        plugins_json = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
        data = _read_json(plugins_json)
        data.setdefault("version", 2)
        plugins = data.setdefault("plugins", {})

        target = str(self.repo.resolve())
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        existing = plugins.get(_PLUGIN_ID, [])
        if existing and existing[0].get("installPath") == target:
            return

        plugins[_PLUGIN_ID] = [
            {
                "scope": "user",
                "installPath": target,
                "version": "local",
                "installedAt": existing[0].get("installedAt", now) if existing else now,
                "lastUpdated": now,
            },
        ]
        _write_json(plugins_json, data)

    @staticmethod
    def enable() -> None:
        """Ensure t3@souliane is enabled in settings.json."""
        resolved = _settings_path()
        data = _read_json(resolved)
        plugins = data.setdefault("enabledPlugins", {})
        if plugins.get(_PLUGIN_ID) is True:
            return
        plugins[_PLUGIN_ID] = True
        _write_json(resolved, data)

    @staticmethod
    def _cleanup_legacy() -> None:
        """Remove legacy symlink-based plugin setup from before marketplace-style registration."""
        plugins_dir = Path.home() / ".claude" / "plugins"
        link = plugins_dir / _PLUGIN_NAME
        if link.is_symlink():
            link.unlink()
            typer.echo(f"OK    Removed legacy plugin symlink: {link}")

        resolved = _settings_path()
        data = _read_json(resolved)
        enabled = data.get("enabledPlugins", {})
        legacy_keys = [k for k in enabled if k.startswith("/") and k.endswith(f"/{_PLUGIN_NAME}")]
        if legacy_keys:
            for key in legacy_keys:
                del enabled[key]
            _write_json(resolved, data)
            typer.echo(f"OK    Removed {len(legacy_keys)} legacy enabledPlugins path entry(ies).")

        cache_root = plugins_dir / "cache" / _MARKETPLACE_NAME / _PLUGIN_NAME
        if cache_root.is_dir():
            shutil.rmtree(cache_root)

    def _ensure_marketplace_symlink(self) -> None:
        """Create ``plugins/t3 -> ..`` inside the repo for marketplace source resolution."""
        plugins_dir = self.repo / "plugins"
        link = plugins_dir / _PLUGIN_NAME
        if link.is_symlink():
            return
        plugins_dir.mkdir(exist_ok=True)
        link.symlink_to("..")

    def _register_marketplace(self) -> None:
        """Ensure the ``souliane`` marketplace is registered in known_marketplaces.json."""
        self._ensure_marketplace_symlink()
        marketplaces_json = Path.home() / ".claude" / "plugins" / "known_marketplaces.json"
        data = _read_json(marketplaces_json)
        target = str(self.repo.resolve())
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        existing = data.get(_MARKETPLACE_NAME, {})
        if existing.get("installLocation") == target:
            return

        data[_MARKETPLACE_NAME] = {
            "source": {"source": "directory", "path": target},
            "installLocation": target,
            "lastUpdated": now,
        }
        _write_json(marketplaces_json, data)
