"""The pre-dispatch quote scan carries a kill-switch that fails LOUD on garbage (#1564).

The ``PreToolUse`` dispatch-quote gate had no off-switch: a false-positive on an
ordinary brief could wedge the fleet with no config escape. This adds
``[teatree] dispatch_quote_scan_enabled`` (default on). The distinctive
requirement is fail-LOUD on an unknown value: a mistyped ``"yes"``/``on``/``2``
must not silently fall back to the default (leaving the operator thinking the
gate is off) — it emits one loud stderr line and keeps the protective default.
"""

from pathlib import Path

import pytest

import hooks.scripts.hook_router as router


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp HOME whose config DB does not exist, so only the TOML tier is read."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "does-not-exist.sqlite3"))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    return tmp_path


def _write_teatree(home_dir: Path, body: str) -> None:
    (home_dir / ".teatree.toml").write_text(f"[teatree]\n{body}", encoding="utf-8")


class TestDispatchQuoteScanEnabledReader:
    def test_default_enabled_without_config(self, home: Path) -> None:
        assert router._dispatch_quote_scan_enabled() is True

    def test_bare_false_disables(self, home: Path) -> None:
        _write_teatree(home, "dispatch_quote_scan_enabled = false\n")
        assert router._dispatch_quote_scan_enabled() is False

    def test_bare_true_keeps_enabled(self, home: Path) -> None:
        _write_teatree(home, "dispatch_quote_scan_enabled = true\n")
        assert router._dispatch_quote_scan_enabled() is True

    def test_unknown_value_warns_loudly_and_keeps_default(self, home: Path, capsys) -> None:
        _write_teatree(home, 'dispatch_quote_scan_enabled = "yes"\n')
        assert router._dispatch_quote_scan_enabled() is True, "an unknown value keeps the protective default"
        err = capsys.readouterr().err
        assert "dispatch_quote_scan_enabled" in err
        assert "not a boolean" in err.lower()

    def test_absent_value_is_silent(self, home: Path, capsys) -> None:
        _write_teatree(home, "some_other_flag = true\n")
        assert router._dispatch_quote_scan_enabled() is True
        assert capsys.readouterr().err == "", "an ABSENT setting is not unknown — no warning"


class TestGateHonoursTheToggle:
    def test_disabled_skips_the_scan_entirely(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(router, "_dispatch_quote_scan_enabled", lambda: False)
        ran: list[dict] = []
        monkeypatch.setattr(router, "_run_dispatch_quote_scanner", lambda data: ran.append(data) or False)
        data = {"tool_name": "Task", "tool_input": {"prompt": "anything at all"}}
        assert router.handle_dispatch_prompt_quote_scanner(data) is False
        assert ran == [], "the scanner must not run when the gate is disabled"

    def test_enabled_runs_the_scan(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(router, "_dispatch_quote_scan_enabled", lambda: True)
        calls: list[dict] = []

        def _record(data: dict) -> bool:
            calls.append(data)
            return False

        monkeypatch.setattr(router, "_run_dispatch_quote_scanner", _record)
        data = {"tool_name": "Task", "tool_input": {"prompt": "anything at all"}}
        assert router.handle_dispatch_prompt_quote_scanner(data) is False
        assert calls == [data], "the scanner must run when the gate is enabled"
