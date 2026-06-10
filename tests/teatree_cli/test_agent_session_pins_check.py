"""``_check_agent_session_pins`` — the `t3 doctor` agent-config gate (teatree#2216).

Validates the ``[agent]`` model + effort settings: a bad ``session_effort``
(off the strict CLI scale) is a hard FAIL surfaced loudly (the parser raises);
an unrecognised model in ``session_model`` or a ``[agent.skill_models]`` floor
is a WARN (it ranks most-capable, so not fatal, but likely a typo). An absent
or all-valid config is silently OK.
"""

from pathlib import Path

import pytest

from teatree.cli._doctor_checks import _check_agent_session_pins


def _write(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


def _patch_config(monkeypatch: pytest.MonkeyPatch, cfg: Path) -> None:
    monkeypatch.setattr("teatree.config_agent.CONFIG_PATH", cfg)


class TestAgentSessionPinsCheck:
    def test_absent_config_is_ok_silent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _patch_config(monkeypatch, tmp_path / "nope.toml")
        assert _check_agent_session_pins() is True
        assert capsys.readouterr().out == ""

    def test_all_valid_is_ok_silent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg = _write(
            tmp_path,
            '[agent]\nsession_model = "fable"\nsession_effort = "xhigh"\n[agent.skill_models]\ncode-review = "opus"\n',
        )
        _patch_config(monkeypatch, cfg)
        assert _check_agent_session_pins() is True
        assert capsys.readouterr().out == ""

    def test_bad_effort_is_hard_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg = _write(tmp_path, '[agent]\nsession_effort = "off"\n')
        _patch_config(monkeypatch, cfg)
        assert _check_agent_session_pins() is False
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "session_effort" in out
        assert "off" in out

    def test_ultracode_effort_is_hard_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # "ultracode" is a session/settings concept, never an effort scale value.
        cfg = _write(tmp_path, '[agent]\nsession_effort = "ultracode"\n')
        _patch_config(monkeypatch, cfg)
        assert _check_agent_session_pins() is False
        assert "FAIL" in capsys.readouterr().out

    def test_unknown_session_model_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg = _write(tmp_path, '[agent]\nsession_model = "gpt-9"\n')
        _patch_config(monkeypatch, cfg)
        assert _check_agent_session_pins() is True
        out = capsys.readouterr().out
        assert "WARN" in out
        assert "gpt-9" in out

    def test_unknown_skill_floor_model_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A floor naming no known tier substring (a real typo) — fabel ≠ fable.
        cfg = _write(tmp_path, '[agent.skill_models]\ncode-review = "fabel"\n')
        _patch_config(monkeypatch, cfg)
        assert _check_agent_session_pins() is True
        out = capsys.readouterr().out
        assert "WARN" in out
        assert "fabel" in out
        assert "code-review" in out

    def test_tier_substring_superstring_is_not_warned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A superstring that contains a known tier (e.g. a dated id) is fine —
        # the system resolves it to that tier by substring, so no false typo WARN.
        cfg = _write(tmp_path, '[agent.skill_models]\nc = "sonnet-4-6"\n')
        _patch_config(monkeypatch, cfg)
        assert _check_agent_session_pins() is True
        assert capsys.readouterr().out == ""

    def test_known_tiers_do_not_warn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg = _write(
            tmp_path,
            '[agent.skill_models]\na = "haiku"\nb = "sonnet"\nc = "opus"\nd = "fable"\n',
        )
        _patch_config(monkeypatch, cfg)
        assert _check_agent_session_pins() is True
        assert capsys.readouterr().out == ""

    def test_dated_full_id_does_not_warn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A dated full id whose tier substring is recognised is fine.
        cfg = _write(tmp_path, '[agent]\nsession_model = "claude-fable-5"\n')
        _patch_config(monkeypatch, cfg)
        assert _check_agent_session_pins() is True
        assert capsys.readouterr().out == ""

    def test_unknown_fable_fallback_warns_when_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # teatree#2237: a typo'd fallback would silently downgrade Fable to an
        # unknown model when the kill-switch is off — WARN on it.
        cfg = _write(tmp_path, '[agent]\nfable_enabled = false\nfable_fallback = "opsu"\n')
        _patch_config(monkeypatch, cfg)
        assert _check_agent_session_pins() is True
        out = capsys.readouterr().out
        assert "WARN" in out
        assert "fable_fallback" in out
        assert "opsu" in out

    def test_valid_fable_fallback_does_not_warn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg = _write(tmp_path, '[agent]\nfable_enabled = false\nfable_fallback = "opus"\n')
        _patch_config(monkeypatch, cfg)
        assert _check_agent_session_pins() is True
        assert capsys.readouterr().out == ""

    def test_bad_fable_fallback_silent_when_enabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The fallback never applies while Fable is enabled, so a typo there is
        # not surfaced — no WARN.
        cfg = _write(tmp_path, '[agent]\nfable_enabled = true\nfable_fallback = "opsu"\n')
        _patch_config(monkeypatch, cfg)
        assert _check_agent_session_pins() is True
        assert capsys.readouterr().out == ""
