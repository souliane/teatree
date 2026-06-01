"""Tests for hook_router loop-cadence consistency (#1036).

``_tick_meta_stale`` (staleness window = cadence*2) and the
loop-registration cron-minutes computation must resolve the loop cadence
through the shared ``teatree.config.cadence_seconds`` resolver, so they
honor ``~/.teatree.toml`` ``loop_cadence_seconds`` and never diverge from
the real slot cadence registered by ``t3 loop``.

Integration-style: real ``hook_router`` helper, real ``teatree.config``
loader pointed at a tmp ``.teatree.toml``; only the clock-dependent
tick-meta mtime is staged on disk.
"""

import json
import time
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import _tick_meta_stale, handle_enforce_loop_on_prompt, handle_enforce_loop_registration


def test_loop_cadence_seconds_honors_toml_when_env_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # #1036: with no T3_LOOP_CADENCE env, the hook cadence must fall back
    # to ~/.teatree.toml loop_cadence_seconds, not the hardcoded 720.
    monkeypatch.delenv("T3_LOOP_CADENCE", raising=False)
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text("[teatree]\nloop_cadence_seconds = 60\n", encoding="utf-8")
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
    from hooks.scripts.hook_router import _loop_cadence_seconds  # noqa: PLC0415

    assert _loop_cadence_seconds() == 60


def test_tick_meta_stale_uses_toml_cadence_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # #1036: staleness window is cadence*2. With toml cadence 60s (env
    # unset), a 200s-old tick-meta is stale (200 > 120). Pre-fix this read
    # env-only -> default 720 -> window 1440s -> 200 < 1440 -> NOT stale,
    # so this asserts the toml-aware behavior (RED pre-fix, GREEN after).
    monkeypatch.delenv("T3_LOOP_CADENCE", raising=False)
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text("[teatree]\nloop_cadence_seconds = 60\n", encoding="utf-8")
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)

    data_home = tmp_path / "xdg"
    meta_dir = data_home / "teatree"
    meta_dir.mkdir(parents=True)
    meta = meta_dir / "tick-meta.json"
    meta.write_text('{"next_epoch": 0, "cadence": 60}\n', encoding="utf-8")
    old = time.time() - 200
    import os  # noqa: PLC0415

    os.utime(meta, (old, old))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

    assert _tick_meta_stale() is True


def test_enforce_loop_on_prompt_emits_toml_cron_minutes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # #1036: the loop-registration cron minutes must match the toml
    # cadence (1800s -> */30). Pre-fix env-only -> 720 -> */12.
    monkeypatch.delenv("T3_LOOP_CADENCE", raising=False)
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text("[teatree]\nloop_cadence_seconds = 1800\n", encoding="utf-8")
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)

    data_home = tmp_path / "xdg"
    (data_home / "teatree").mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(router, "STATE_DIR", state)

    handle_enforce_loop_on_prompt({"session_id": "s-1036"})
    out = capsys.readouterr().out
    assert "*/30 * * * *" in out


def test_enforce_loop_registration_uses_toml_cron_minutes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # #1036: the PreToolUse deny reason's cron minutes must also match the
    # toml cadence (1800s -> */30). Covers the third hook_router reader.
    monkeypatch.delenv("T3_LOOP_CADENCE", raising=False)
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text("[teatree]\nloop_cadence_seconds = 1800\n", encoding="utf-8")
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(router, "STATE_DIR", state)
    (state / "s-1036.loop-pending").write_text("1", encoding="utf-8")

    blocked = handle_enforce_loop_registration({"session_id": "s-1036", "tool_name": "Bash"})
    assert blocked is True
    assert "*/30 * * * *" in capsys.readouterr().out


def test_loop_registration_exempts_subagents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # NEVER-LOCKOUT: a sub-agent (non-empty agent_id) has no CronCreate tool, so
    # a deny here is an unrecoverable lockout — every spawned coder/reviewer was
    # killed in the incident. The same call WITHOUT agent_id must still block the
    # main session, proving the exemption is exactly what unblocks the sub-agent.
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(router, "STATE_DIR", state)
    (state / "sub.loop-pending").write_text("1", encoding="utf-8")

    subagent = {"session_id": "sub", "tool_name": "Bash", "agent_id": "sub-1"}
    assert handle_enforce_loop_registration(subagent) is False

    main_session = {"session_id": "sub", "tool_name": "Bash"}
    assert handle_enforce_loop_registration(main_session) is True


def test_loop_registration_kill_switch_disables_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # NEVER-LOCKOUT: the durable [teatree] loop_registration_gate_enabled = false
    # kill-switch disables the gate with no code edit, even with a pending marker.
    # The autouse _isolate_env fixture already routes HOME (hence Path.home()) at
    # tmp_path/home, so the config write lands where the gate reads it.
    home = tmp_path / "home"
    (home / ".teatree.toml").write_text("[teatree]\nloop_registration_gate_enabled = false\n", encoding="utf-8")

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(router, "STATE_DIR", state)
    (state / "off.loop-pending").write_text("1", encoding="utf-8")

    assert router._loop_registration_gate_enabled() is False
    blocked = handle_enforce_loop_registration({"session_id": "off", "tool_name": "Bash"})
    assert blocked is False


def test_loop_registration_gate_enabled_defaults_true_without_config(tmp_path: Path) -> None:
    # Fails OPEN to enabled on a missing/unset config so the nudge keeps working
    # by default; only an explicit false disables it. HOME is the conftest-
    # isolated tmp_path/home, which has no .teatree.toml.
    assert router._loop_registration_gate_enabled() is True

    (tmp_path / "home" / ".teatree.toml").write_text("[teatree]\n", encoding="utf-8")
    assert router._loop_registration_gate_enabled() is True


def test_loop_registration_reason_is_ux_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # NEVER-LOCKOUT backstop: the deny reason starts with the LOOP REGISTRATION
    # UX-gate prefix, so the repeated-denial circuit breaker auto-relaxes it
    # instead of blocking forever.
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(router, "STATE_DIR", state)
    (state / "ux.loop-pending").write_text("1", encoding="utf-8")

    blocked = handle_enforce_loop_registration({"session_id": "ux", "tool_name": "Bash"})
    assert blocked is True
    payload = capsys.readouterr().out
    reason = json.loads(payload.strip())["permissionDecisionReason"]
    assert reason.startswith("LOOP REGISTRATION")
    assert router._deny_is_ux_gate(reason) is True


def test_loop_cadence_seconds_falls_back_to_env_when_teatree_unimportable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #1036: best-effort — if teatree.config cannot resolve, the helper
    # falls back to the env-only read (covers the except branch).
    monkeypatch.setenv("T3_LOOP_CADENCE", "240")

    def _boom() -> int:
        msg = "teatree unavailable in this hook process"
        raise RuntimeError(msg)

    monkeypatch.setattr("teatree.config.cadence_seconds", _boom)
    from hooks.scripts.hook_router import _loop_cadence_seconds  # noqa: PLC0415

    assert _loop_cadence_seconds() == 240


def test_loop_cadence_seconds_inserts_src_on_path_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # #1036: covers the sys.path-insert + finally-cleanup branch taken
    # when the hook process does not already have teatree's src on path.
    import sys  # noqa: PLC0415

    src_dir = str(Path(router.__file__).resolve().parents[2] / "src")
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != src_dir])
    monkeypatch.delenv("T3_LOOP_CADENCE", raising=False)
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text("[teatree]\nloop_cadence_seconds = 120\n", encoding="utf-8")
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
    from hooks.scripts.hook_router import _loop_cadence_seconds  # noqa: PLC0415

    assert _loop_cadence_seconds() == 120
    assert src_dir not in sys.path
