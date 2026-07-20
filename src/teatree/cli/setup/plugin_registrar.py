"""Claude-plugin and marketplace registration for ``t3 setup``."""

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import typer

from teatree.utils.run import TimeoutExpired, run_allowed_to_fail

_PLUGIN_NAME = "t3"
_MARKETPLACE_NAME = "souliane"
_PLUGIN_ID = f"{_PLUGIN_NAME}@{_MARKETPLACE_NAME}"

_PYRIGHT_MARKETPLACE = "claude-plugins-official"
_PYRIGHT_MARKETPLACE_SOURCE = "anthropics/claude-plugins-official"
_PYRIGHT_PLUGIN_NAME = "pyright-lsp"
_PYRIGHT_PLUGIN_ID = f"{_PYRIGHT_PLUGIN_NAME}@{_PYRIGHT_MARKETPLACE}"

# Bound for a ``claude plugin`` CLI call — it clones + validates the remote
# marketplace / plugin, so an unreachable network must time out and continue
# rather than hang setup.
_CLAUDE_CLI_TIMEOUT_S = 120


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


def _set_enabled_plugin(plugin_id: str) -> bool:
    """Ensure ``plugin_id`` is enabled in settings.json; return True when it changed."""
    resolved = _settings_path()
    data = _read_json(resolved)
    plugins = data.setdefault("enabledPlugins", {})
    if plugins.get(plugin_id) is True:
        return False
    plugins[plugin_id] = True
    _write_json(resolved, data)
    return True


def _plugin_installed(plugin_id: str) -> bool:
    """True when ``plugin_id`` has an installed_plugins.json entry with a live ``installPath``."""
    plugins = _read_json(Path.home() / ".claude" / "plugins" / "installed_plugins.json").get("plugins", {})
    entries = plugins.get(plugin_id) if isinstance(plugins, dict) else None
    if not (isinstance(entries, list) and entries):
        return False
    first = entries[0]
    install_path = first.get("installPath") if isinstance(first, dict) else None
    return isinstance(install_path, str) and bool(install_path) and Path(install_path).is_dir()


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


class PyrightPluginRegistrar:
    """Register + enable the external ``pyright-lsp`` plugin for live type diagnostics.

    Unlike :class:`PluginRegistrar` — which writes the plugin JSON directly for the
    LOCAL ``souliane`` marketplace clone — ``pyright-lsp`` lives in the remote
    ``anthropics/claude-plugins-official`` marketplace, whose on-disk cache is a git
    clone Claude Code manages. Registration therefore goes through the ``claude
    plugin`` CLI (the same mechanism Claude Code itself uses): ``marketplace add``
    clones + validates the marketplace, ``install`` clones the plugin into the cache
    and enables it. Both are idempotent. Offline-safe: an unreachable marketplace
    WARNs and returns ``False`` (setup continues) rather than aborting — matching the
    other best-effort setup steps.

    The plugin gives factory agents LIVE pyright type diagnostics while coding, so a
    type error surfaces in-session instead of only at CI. Its language server
    (``pyright-langserver``, from the npm ``pyright`` package) must be on PATH for the
    plugin to start — ``t3 doctor`` advisory-WARNs when it is not.
    """

    def install(self) -> bool:
        """Register + enable ``pyright-lsp`` via the ``claude plugin`` CLI (idempotent, offline-safe)."""
        if _plugin_installed(_PYRIGHT_PLUGIN_ID):
            _set_enabled_plugin(_PYRIGHT_PLUGIN_ID)
            typer.echo(f"OK    Plugin {_PYRIGHT_PLUGIN_ID} already registered — enabled.")
            return True
        claude = shutil.which("claude")
        if claude is None:
            typer.echo("WARN  `claude` not on PATH — skipped pyright-lsp plugin registration; setup continues.")
            return False
        if not self._run_claude(claude, "plugin", "marketplace", "add", _PYRIGHT_MARKETPLACE_SOURCE):
            typer.echo(
                f"WARN  Could not add the {_PYRIGHT_MARKETPLACE} marketplace (offline?) — "
                "pyright-lsp skipped; setup continues.",
            )
            return False
        if not self._run_claude(claude, "plugin", "install", _PYRIGHT_PLUGIN_ID):
            typer.echo("WARN  Could not install pyright-lsp (offline?) — skipped; setup continues.")
            return False
        _set_enabled_plugin(_PYRIGHT_PLUGIN_ID)
        typer.echo(f"OK    Plugin {_PYRIGHT_PLUGIN_ID} registered + enabled for live pyright diagnostics.")
        return True

    @staticmethod
    def _run_claude(claude: str, *args: str) -> bool:
        """Run ``claude <args>`` via the audited wrapper; return True on exit 0.

        ``expected_codes=None`` accepts any exit code (this method judges success on
        the return code itself), so a non-zero exit is a non-fatal ``False`` rather
        than a raise. A timeout or spawn error (``claude`` vanished) is likewise a
        non-fatal ``False`` so an unreachable marketplace never aborts setup.
        """
        try:
            result = run_allowed_to_fail([claude, *args], expected_codes=None, timeout=_CLAUDE_CLI_TIMEOUT_S)
        except (OSError, TimeoutExpired):
            return False
        return result.returncode == 0
