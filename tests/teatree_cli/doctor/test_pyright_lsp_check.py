"""Tests for ``_check_pyright_lsp_plugin`` — enabled-but-unprovisioned LSP gate (#3568).

pyright-lsp gives factory agents LIVE pyright type diagnostics while coding. Under
the epic #3445 "enabled but not provisioned → FAIL" principle the check hard-FAILs
when the plugin is ENABLED but its ``pyright-langserver`` binary is not on PATH — the
LSP would silently never start. The plugin merely being disabled is a config choice,
so that case stays an advisory WARN.
"""

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

from teatree.cli.doctor.checks_resources import _check_pyright_lsp_plugin

_PLUGIN_ID = "pyright-lsp@claude-plugins-official"


def _write_settings(home: Path, enabled: dict[str, object]) -> None:
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({"enabledPlugins": enabled}), encoding="utf-8")


def _run(home: Path, *, langserver_on_path: bool) -> tuple[bool, str]:
    def _which(name: str) -> str | None:
        return f"/usr/bin/{name}" if langserver_on_path else None

    out = io.StringIO()
    with redirect_stdout(out):
        ok = _check_pyright_lsp_plugin(home=home, which=_which)
    return ok, out.getvalue()


class TestCheckPyrightLspPlugin:
    def test_silent_pass_when_enabled_and_langserver_present(self, tmp_path: Path) -> None:
        _write_settings(tmp_path, {_PLUGIN_ID: True})
        ok, message = _run(tmp_path, langserver_on_path=True)
        assert ok is True
        assert message.strip() == ""

    def test_warns_when_plugin_not_enabled(self, tmp_path: Path) -> None:
        _write_settings(tmp_path, {"t3@souliane": True})
        ok, message = _run(tmp_path, langserver_on_path=True)
        assert ok is True  # advisory only — never gates
        assert "WARN" in message
        assert "pyright-lsp" in message
        assert "t3 setup" in message
        assert "FAIL" not in message

    def test_fails_when_enabled_but_langserver_missing(self, tmp_path: Path) -> None:
        _write_settings(tmp_path, {_PLUGIN_ID: True})
        ok, message = _run(tmp_path, langserver_on_path=False)
        assert ok is False  # enabled-but-unprovisioned is a hard FAIL (#3568)
        assert "FAIL" in message
        assert "pyright-langserver" in message
        assert "npm install -g --prefix ~/.local pyright" in message

    def test_never_gates_when_settings_absent(self, tmp_path: Path) -> None:
        # No ~/.claude/settings.json at all → treated as "not enabled", WARN only.
        ok, message = _run(tmp_path, langserver_on_path=True)
        assert ok is True
        assert "FAIL" not in message

    def test_never_gates_when_settings_absent_even_without_langserver(self, tmp_path: Path) -> None:
        # Plugin disabled AND no binary → the disabled config choice never FAILs.
        ok, message = _run(tmp_path, langserver_on_path=False)
        assert ok is True
        assert "FAIL" not in message
