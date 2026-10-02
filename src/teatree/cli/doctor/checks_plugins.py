"""Doctor checks for agent plugins and their local settings files."""

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import cast

import typer

type JsonObject = dict[str, object]

_PYRIGHT_PLUGIN_ID = "pyright-lsp@claude-plugins-official"
_PYRIGHT_LANGSERVER = "pyright-langserver"
_PYRIGHT_INSTALL_CMD = "npm install -g --prefix ~/.local pyright"


def _read_json_object(path: Path) -> JsonObject:
    """Load ``path`` as a JSON object, or ``{}`` when absent/unreadable/not an object."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _check_pyright_lsp_plugin(*, home: Path | None = None, which: Callable[[str], str | None] | None = None) -> bool:
    """FAIL when the pyright-lsp plugin is enabled but its langserver is absent.

    A disabled plugin is an advisory WARN, while enabled-but-unprovisioned is
    a hard failure: the language server would silently never start (#3568).
    """
    resolve = shutil.which if which is None else which
    try:
        enabled = _read_json_object((home or Path.home()) / ".claude" / "settings.json").get("enabledPlugins")
    except OSError:
        return True
    if not (isinstance(enabled, dict) and cast("JsonObject", enabled).get(_PYRIGHT_PLUGIN_ID) is True):
        typer.echo(
            "WARN  pyright-lsp plugin is not enabled — factory agents get no LIVE pyright type "
            "diagnostics while coding (type errors surface only at CI). Run `t3 setup` to register "
            "+ enable it (advisory only; nothing is gated)."
        )
        return True
    if resolve(_PYRIGHT_LANGSERVER) is None:
        typer.echo(
            f"FAIL  pyright-lsp plugin is enabled but `{_PYRIGHT_LANGSERVER}` is not on PATH — the "
            "language server cannot start, so the enabled LSP silently delivers no live type "
            f"diagnostics. Install it: `{_PYRIGHT_INSTALL_CMD}` (or re-run `t3 setup`)."
        )
        return False
    return True
