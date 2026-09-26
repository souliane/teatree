"""Register the shared TeaTree plugin with Codex's local marketplace for ``t3 setup``.

Codex is registered against a slim, manifest-derived tree under the teatree data
dir (:mod:`teatree.cli.setup.codex_plugin_payload`), never the checkout. A failure
is reported with the Codex CLI's own exit code and stderr, so ``t3 setup`` never
fails on a bare "could not install".
"""

import json
import shutil
from collections.abc import Mapping
from pathlib import Path

import typer

from teatree.cli.setup.codex_plugin_payload import CodexPayloadError, CodexPluginPayload
from teatree.cli.setup.codex_plugin_staging import codex_home, reclaiming_new_staging
from teatree.cli.setup.plugin_registrar import MARKETPLACE_NAME, PLUGIN_CLI_TIMEOUT_S, PLUGIN_ID, PLUGIN_NAME
from teatree.paths import get_data_dir
from teatree.utils.run import CompletedProcess, TimeoutExpired, run_allowed_to_fail

type JsonObject = dict[str, object]

_STDERR_TAIL_CHARS = 1000


class CodexCliError(RuntimeError):
    pass


class CodexPluginRegistrar:
    def __init__(self, repo: Path) -> None:
        self.manifest_root = next(
            (root for root in (repo, repo / "vendor" / "teatree") if self._has_manifests(root)), repo
        )

    @staticmethod
    def _has_manifests(root: Path) -> bool:
        try:
            marketplace = json.loads((root / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8"))
            plugin = json.loads((root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            return False
        if not isinstance(marketplace, dict) or not isinstance(plugin, dict):
            return False
        entries = marketplace.get("plugins")
        return (
            marketplace.get("name") == MARKETPLACE_NAME
            and isinstance(entries, list)
            and any(
                isinstance(entry, dict)
                and entry.get("name") == PLUGIN_NAME
                and isinstance(entry.get("source"), dict)
                and entry["source"].get("path") == f"./plugins/{PLUGIN_NAME}"
                for entry in entries
            )
            and plugin.get("name") == PLUGIN_NAME
            and isinstance(plugin.get("skills"), str)
        )

    def install(self) -> bool:
        codex = shutil.which("codex")
        if codex is None:
            typer.echo("WARN  `codex` not on PATH — skipped TeaTree Codex plugin registration; setup continues.")
            return False
        if not self._has_manifests(self.manifest_root):
            typer.echo(
                "WARN  TeaTree Codex plugin manifests are missing or invalid — registration skipped before Codex CLI."
            )
            return False
        try:
            plugin_root = CodexPluginPayload(self.manifest_root, get_data_dir("codex-plugin")).materialize().resolve()
            self._ensure_marketplace(codex, plugin_root)
            self._ensure_plugin(codex, plugin_root)
        except (CodexPayloadError, CodexCliError) as exc:
            typer.echo(f"WARN  {exc} — setup continues.")
            return False
        typer.echo(f"OK    TeaTree Codex plugin registered from {plugin_root} with shared skills and MCP tools.")
        return True

    def _ensure_marketplace(self, codex: str, plugin_root: Path) -> None:
        payload = self._json(codex, "inspect Codex marketplaces", "plugin", "marketplace", "list")
        if self._marketplace_registered(payload, plugin_root):
            return
        if self._marketplace_present(payload):
            self._call(
                codex, "replace the TeaTree Codex marketplace", "plugin", "marketplace", "remove", MARKETPLACE_NAME
            )
        self._call(codex, "add the TeaTree Codex marketplace", "plugin", "marketplace", "add", str(plugin_root))

    def _ensure_plugin(self, codex: str, plugin_root: Path) -> None:
        payload = self._json(codex, "inspect Codex plugins", "plugin", "list")
        if self._plugin_registered(payload, plugin_root):
            return
        if self._plugin_present(payload):
            self._call(codex, "replace the TeaTree Codex plugin", "plugin", "remove", PLUGIN_ID)
        with reclaiming_new_staging(codex_home()):
            self._call(codex, "install the TeaTree Codex plugin", "plugin", "add", PLUGIN_ID)

    @staticmethod
    def _marketplace_present(payload: Mapping[str, object]) -> bool:
        marketplaces = payload.get("marketplaces", [])
        return isinstance(marketplaces, list) and any(
            isinstance(entry, dict) and entry.get("name") == MARKETPLACE_NAME for entry in marketplaces
        )

    @staticmethod
    def _marketplace_registered(payload: Mapping[str, object], plugin_root: Path) -> bool:
        marketplaces = payload.get("marketplaces", [])
        if not isinstance(marketplaces, list):
            return False
        return any(
            isinstance(entry, dict)
            and entry.get("name") == MARKETPLACE_NAME
            and isinstance(entry.get("root"), str)
            and Path(entry["root"]).resolve() == plugin_root.resolve()
            for entry in marketplaces
        )

    @staticmethod
    def _plugin_present(payload: Mapping[str, object]) -> bool:
        plugins = payload.get("installed", [])
        return isinstance(plugins, list) and any(
            isinstance(entry, dict) and entry.get("pluginId") == PLUGIN_ID and entry.get("installed") is True
            for entry in plugins
        )

    @staticmethod
    def _plugin_registered(payload: Mapping[str, object], plugin_root: Path) -> bool:
        expected = {plugin_root.resolve(), (plugin_root / "plugins" / PLUGIN_NAME).resolve()}
        plugins = payload.get("installed", [])
        if not isinstance(plugins, list):
            return False
        for entry in plugins:
            if not isinstance(entry, dict) or entry.get("pluginId") != PLUGIN_ID or entry.get("installed") is not True:
                continue
            if entry.get("enabled") is not True:
                continue
            source = entry.get("source")
            path = source.get("path") if isinstance(source, dict) else None
            if isinstance(path, str) and Path(path).resolve() in expected:
                return True
        return False

    @classmethod
    def _json(cls, codex: str, purpose: str, *args: str) -> JsonObject:
        stdout = cls._call(codex, purpose, *args).stdout
        decoder = json.JSONDecoder()
        offsets = (index for index, character in enumerate(stdout) if character in "[{")
        for offset in reversed(tuple(offsets)):
            try:
                payload, end = decoder.raw_decode(stdout, offset)
            except json.JSONDecodeError:
                continue
            if not stdout[end:].strip() and isinstance(payload, dict):
                return payload
        msg = f"Could not {purpose}: `codex {' '.join(args)} --json` printed no JSON object ({_tail(stdout)})"
        raise CodexCliError(msg)

    @staticmethod
    def _call(codex: str, purpose: str, *args: str) -> CompletedProcess[str]:
        command = [*args, "--json"]
        shown = f"codex {' '.join(command)}"
        try:
            result = run_allowed_to_fail([codex, *command], expected_codes=None, timeout=PLUGIN_CLI_TIMEOUT_S)
        except TimeoutExpired:
            msg = f"Could not {purpose}: `{shown}` timed out after {PLUGIN_CLI_TIMEOUT_S}s"
            raise CodexCliError(msg) from None
        except OSError as exc:
            msg = f"Could not {purpose}: `{shown}` did not start ({exc})"
            raise CodexCliError(msg) from exc
        if result.returncode != 0:
            msg = f"Could not {purpose}: `{shown}` exited {result.returncode}: {_tail(result.stderr or result.stdout)}"
            raise CodexCliError(msg)
        return result


def _tail(output: str) -> str:
    flattened = " ".join(output.split())
    if not flattened:
        return "no output"
    return flattened if len(flattened) <= _STDERR_TAIL_CHARS else f"…{flattened[-_STDERR_TAIL_CHARS:]}"
