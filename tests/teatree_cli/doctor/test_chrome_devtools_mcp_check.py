"""Tests for ``_check_chrome_devtools_mcp_suggestion`` — optional e2e aid (#3271).

chrome-devtools MCP gives an interactive DOM/console/network view that makes
authoring and debugging Playwright e2e specs tractable. It is a pure
developer-experience aid: teatree's runtime requires zero MCP. The check emits
an ``INFO`` suggestion only when it is absent, and never gates the doctor exit
code (its absence must never fail or WARN).
"""

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

from teatree.cli.doctor.checks_mcp import _check_chrome_devtools_mcp_suggestion
from teatree.core.evidence.browser_diagnosis import chrome_devtools_add_command


def _run(home: Path) -> tuple[bool, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        ok = _check_chrome_devtools_mcp_suggestion(home=home, cwd=home)
    return ok, out.getvalue()


def _write_claude_json(home: Path, servers: dict[str, object]) -> None:
    (home / ".claude.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


class TestCheckChromeDevtoolsMcpSuggestion:
    def test_suggests_at_info_level_when_absent(self, tmp_path: Path) -> None:
        _write_claude_json(tmp_path, {"some-other": {"command": "x"}})
        ok, message = _run(tmp_path)
        assert ok is True
        assert "INFO" in message
        assert "chrome-devtools" in message

    def test_suggested_command_is_headless(self, tmp_path: Path) -> None:
        _write_claude_json(tmp_path, {})
        _, message = _run(tmp_path)
        assert "--headless=true" in message

    def test_suggested_command_tracks_the_shared_constant(self, tmp_path: Path) -> None:
        # A hardcoded duplicate of the registration line silently drifts from the canonical one.
        _write_claude_json(tmp_path, {})
        _, message = _run(tmp_path)
        assert chrome_devtools_add_command() in message

    def test_never_warns_or_fails_when_absent(self, tmp_path: Path) -> None:
        _write_claude_json(tmp_path, {})
        ok, message = _run(tmp_path)
        assert ok is True  # its absence must never gate anything
        assert "WARN" not in message
        assert "FAIL" not in message

    def test_silent_when_chrome_devtools_configured(self, tmp_path: Path) -> None:
        _write_claude_json(tmp_path, {"chrome-devtools": {"command": "npx"}})
        ok, message = _run(tmp_path)
        assert ok is True
        assert message.strip() == ""

    def test_never_gates_on_missing_config(self, tmp_path: Path) -> None:
        # No ~/.claude.json at all → treated as "absent", INFO only, never a crash/gate.
        ok, message = _run(tmp_path)
        assert ok is True
        assert "WARN" not in message
        assert "FAIL" not in message

    def test_degrades_to_silent_pass_when_read_raises(self, tmp_path: Path, monkeypatch) -> None:
        def _boom(**_kwargs: object) -> list[object]:
            msg = "mcp read exploded"
            raise RuntimeError(msg)

        monkeypatch.setattr("teatree.core.mcp_connectivity.read_enabled_mcp_servers", _boom, raising=True)
        ok, message = _run(tmp_path)
        assert ok is True  # an optional suggestion never crashes or gates the doctor run
        assert message.strip() == ""
