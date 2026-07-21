"""Tests for ``_check_pyright_lsp_plugin`` — advisory live-type-diagnostics aid.

pyright-lsp gives factory agents LIVE pyright type diagnostics while coding, so a
type error surfaces in-session instead of only at CI. It is a productivity aid, not
a worker gate: the check WARNs when the plugin is not enabled, or when its
``pyright-langserver`` is missing from PATH, but NEVER gates the doctor exit code
(always returns ``True``) and never FAILs.
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

    def test_warns_when_langserver_missing_from_path(self, tmp_path: Path) -> None:
        _write_settings(tmp_path, {_PLUGIN_ID: True})
        ok, message = _run(tmp_path, langserver_on_path=False)
        assert ok is True  # advisory only — never gates
        assert "WARN" in message
        assert "pyright-langserver" in message
        assert "npm install -g pyright" in message
        assert "FAIL" not in message

    def test_never_gates_when_settings_absent(self, tmp_path: Path) -> None:
        # No ~/.claude/settings.json at all → treated as "not enabled", WARN only.
        ok, message = _run(tmp_path, langserver_on_path=True)
        assert ok is True
        assert "FAIL" not in message
