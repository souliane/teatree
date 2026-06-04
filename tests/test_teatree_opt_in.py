"""Tests for teatree opt-in: marker mechanism, gated injection points.

Covers must-fire / must-NOT-fire directions for:
1. Fresh session (no marker) -- injection points silent, loop-registration exempt.
2. Marker present -- injection points fire as before.
3. handle_track_skill_usage sets marker for t3:teatree and for skills that
    require: [teatree] (closure expansion).
4. Risk-6: mid-session teatree load triggers ownership claim from
    handle_enforce_loop_on_prompt when the loop is not disabled.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import (
    _OWNER_LOOP,
    _loop_registration_exempt,
    _read_loop_registry,
    _teatree_active,
    _write_loop_registry,
    handle_enforce_loop_on_prompt,
    handle_session_start_bootstrap,
    handle_track_skill_usage,
)

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(router, "STATE_DIR", state)

    reg_dir = tmp_path / "data"
    reg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(reg_dir))

    monkeypatch.setattr(router, "_TTY_PATH", str(tmp_path / "fake-tty"))
    monkeypatch.setenv("TEATREE_BASH_ENV_FILE", str(tmp_path / "no-bash-env"))


def _mark_active(session_id: str) -> None:
    (router.STATE_DIR / f"{session_id}.teatree-active").touch()


def _is_marked_active(session_id: str) -> bool:
    return (router.STATE_DIR / f"{session_id}.teatree-active").is_file()


def _live_pid() -> int:
    return os.getpid()


# ── _teatree_active helper ─────────────────────────────────────────────


class TestTeatreeActiveHelper:
    def test_returns_false_when_no_marker(self) -> None:
        assert _teatree_active("fresh-session") is False

    def test_returns_false_for_empty_session_id(self) -> None:
        assert _teatree_active("") is False

    def test_returns_true_when_marker_exists(self) -> None:
        _mark_active("active-session")
        assert _teatree_active("active-session") is True

    def test_different_session_is_not_active(self) -> None:
        _mark_active("session-a")
        assert _teatree_active("session-b") is False


# ── handle_session_start_bootstrap gating ─────────────────────────────


class TestSessionStartBootstrapGating:
    def test_fresh_session_without_marker_emits_nothing(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_session_start_bootstrap({"session_id": "no-teatree"})
        assert capsys.readouterr().out == ""

    def test_fresh_session_without_marker_does_not_claim_ownership(self) -> None:
        handle_session_start_bootstrap({"session_id": "no-teatree"})
        assert _read_loop_registry() == {}

    def test_marked_session_emits_tick_dispatch_directive(self, capsys: pytest.CaptureFixture[str]) -> None:
        _mark_active("teatree-session")
        handle_session_start_bootstrap({"session_id": "teatree-session"})
        out = capsys.readouterr().out
        assert out != ""
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "t3 loop tick" in ctx

    def test_marked_session_claims_ownership(self) -> None:
        _mark_active("teatree-session")
        handle_session_start_bootstrap({"session_id": "teatree-session"})
        owner = _read_loop_registry().get(_OWNER_LOOP)
        assert owner is not None
        assert owner["session_id"] == "teatree-session"

    def test_post_compaction_same_session_with_marker_still_fires(self, capsys: pytest.CaptureFixture[str]) -> None:
        _mark_active("compact-session")
        _write_loop_registry(
            {
                _OWNER_LOOP: {
                    "session_id": "compact-session",
                    "agent_id": "",
                    "pid": _live_pid(),
                }
            }
        )
        handle_session_start_bootstrap({"session_id": "compact-session", "source": "compact"})
        out = capsys.readouterr().out
        assert out != ""
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "t3 loop tick" in ctx


# ── handle_enforce_loop_on_prompt gating ──────────────────────────────


class TestEnforceLoopOnPromptGating:
    def test_fresh_session_without_marker_emits_nothing(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_enforce_loop_on_prompt({"session_id": "no-teatree"})
        out = capsys.readouterr().out
        assert out == ""

    def test_marked_session_with_stale_tick_emits_register_cron(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mark_active("teatree-session")
        monkeypatch.setattr(router, "_tick_meta_stale", lambda: True)
        monkeypatch.setattr(router, "_session_has_loop", lambda sid: False)
        handle_enforce_loop_on_prompt({"session_id": "teatree-session"})
        out = capsys.readouterr().out
        assert out != ""
        assert "register_cron" in out


# ── _loop_registration_exempt gating ─────────────────────────────────


class TestLoopRegistrationExemptGating:
    def test_fresh_session_without_marker_is_exempt(self) -> None:
        assert _loop_registration_exempt({"session_id": "no-teatree", "tool_name": "Bash"}) is True

    def test_unmarked_loop_driver_is_exempt_only_because_of_teatree_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Anti-vacuous complement: an UNMARKED session that WOULD be the loop
        # driver (empty registry => _session_drives_loop True) and that clears
        # every other exemption (gate enabled, Bash tool, not a sub-agent) must
        # still be exempt PURELY because the teatree-active gate fires first.
        # Goes RED if the _teatree_active early-return is removed.
        monkeypatch.setattr(router, "_loop_registration_gate_enabled", lambda: True)
        assert _read_loop_registry() == {}
        assert router._session_drives_loop("unmarked-driver") is True
        result = _loop_registration_exempt({"session_id": "unmarked-driver", "tool_name": "Bash"})
        assert result is True

    def test_marked_loop_driver_is_not_exempt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Symmetric must-fire: a MARKED session that is the loop driver clears
        # the teatree gate and is genuinely non-exempt (must be nagged).
        _mark_active("teatree-session")
        monkeypatch.setattr(router, "_loop_registration_gate_enabled", lambda: True)
        assert _read_loop_registry() == {}
        assert router._session_drives_loop("teatree-session") is True
        result = _loop_registration_exempt({"session_id": "teatree-session", "tool_name": "Bash"})
        assert result is False

    def test_marked_non_driver_session_is_exempt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A MARKED session that a DIFFERENT live session owns is not the driver,
        # so it is exempt despite passing the teatree gate.
        _mark_active("teatree-session")
        _write_loop_registry(
            {
                _OWNER_LOOP: {
                    "session_id": "other-live-owner",
                    "agent_id": "",
                    "pid": _live_pid(),
                }
            }
        )
        monkeypatch.setattr(router, "_loop_registration_gate_enabled", lambda: True)
        result = _loop_registration_exempt({"session_id": "teatree-session", "tool_name": "Bash"})
        assert result is True

    def test_no_session_id_is_always_exempt(self) -> None:
        assert _loop_registration_exempt({"tool_name": "Bash"}) is True


# ── handle_track_skill_usage sets marker ──────────────────────────────


class TestTrackSkillUsageSetsMarker:
    def test_teatree_skill_sets_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            router,
            "_resolve_skill_closure",
            lambda skills: skills,
        )
        handle_track_skill_usage(
            {
                "session_id": "track-sess",
                "tool_name": "Skill",
                "tool_input": {"skill": "t3:teatree"},
            }
        )
        assert _is_marked_active("track-sess")

    def test_bare_teatree_skill_name_sets_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            router,
            "_resolve_skill_closure",
            lambda skills: skills,
        )
        handle_track_skill_usage(
            {
                "session_id": "track-sess2",
                "tool_name": "Skill",
                "tool_input": {"skill": "teatree"},
            }
        )
        assert _is_marked_active("track-sess2")

    def test_non_teatree_skill_does_not_set_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            router,
            "_resolve_skill_closure",
            lambda skills: skills,
        )
        handle_track_skill_usage(
            {
                "session_id": "track-sess3",
                "tool_name": "Skill",
                "tool_input": {"skill": "t3:code"},
            }
        )
        assert not _is_marked_active("track-sess3")

    def test_closure_with_teatree_sets_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            router,
            "_resolve_skill_closure",
            lambda skills: [*list(skills), "t3:teatree"],
        )
        handle_track_skill_usage(
            {
                "session_id": "track-sess4",
                "tool_name": "Skill",
                "tool_input": {"skill": "t3:ticket"},
            }
        )
        assert _is_marked_active("track-sess4")

    def test_marker_is_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            router,
            "_resolve_skill_closure",
            lambda skills: skills,
        )
        for _ in range(3):
            handle_track_skill_usage(
                {
                    "session_id": "track-sess5",
                    "tool_name": "Skill",
                    "tool_input": {"skill": "t3:teatree"},
                }
            )
        assert _is_marked_active("track-sess5")

    def test_instructions_loaded_with_teatree_sets_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            router,
            "_resolve_skill_closure",
            lambda skills: skills,
        )
        handle_track_skill_usage(
            {
                "session_id": "track-sess6",
                "skills": [{"name": "t3:teatree"}],
            }
        )
        assert _is_marked_active("track-sess6")


# ── Real closure: cross-cutting skills must NOT drag in teatree (FINDING B) ──

_SKILLS_TREE = Path(__file__).resolve().parents[1] / "skills"
# Lifecycle skills shared with downstream overlays: loading one in an overlay
# session must NOT activate teatree's loop machinery.
_CROSS_CUTTING_SKILLS = [
    "code",
    "ticket",
    "review",
    "ship",
    "test",
    "followup",
    "handover",
    "next",
    "review-request",
    "loops",
]
# Genuinely teatree-specific skills keep the transitive opt-in.
_TEATREE_SPECIFIC_SKILLS = ["teatree-dogfood", "teatree-batch", "teatree-plan"]


class TestRealClosureMarkerActivation:
    @pytest.fixture(autouse=True)
    def _real_skill_tree(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_SKILL_SEARCH_DIRS", str(_SKILLS_TREE))

    @pytest.mark.parametrize("skill", _CROSS_CUTTING_SKILLS)
    def test_cross_cutting_skill_does_not_set_marker(self, skill: str) -> None:
        session = f"overlay-{skill}"
        handle_track_skill_usage(
            {
                "session_id": session,
                "tool_name": "Skill",
                "tool_input": {"skill": f"t3:{skill}"},
            }
        )
        assert not _is_marked_active(session)

    @pytest.mark.parametrize("skill", _TEATREE_SPECIFIC_SKILLS)
    def test_teatree_specific_skill_sets_marker(self, skill: str) -> None:
        session = f"tt-{skill}"
        handle_track_skill_usage(
            {
                "session_id": session,
                "tool_name": "Skill",
                "tool_input": {"skill": f"t3:{skill}"},
            }
        )
        assert _is_marked_active(session)

    def test_direct_teatree_skill_sets_marker(self) -> None:
        handle_track_skill_usage(
            {
                "session_id": "tt-direct",
                "tool_name": "Skill",
                "tool_input": {"skill": "t3:teatree"},
            }
        )
        assert _is_marked_active("tt-direct")


# ── Risk-6: mid-session ownership claim from prompt handler ───────────


class TestRisk6MidSessionOwnershipClaim:
    def test_marked_session_claims_ownership_when_no_live_owner(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mark_active("mid-sess")
        monkeypatch.setattr(router, "_tick_meta_stale", lambda: True)
        monkeypatch.setattr(router, "_session_has_loop", lambda sid: False)

        handle_enforce_loop_on_prompt({"session_id": "mid-sess"})

        owner = _read_loop_registry().get(_OWNER_LOOP)
        assert owner is not None
        assert owner["session_id"] == "mid-sess"

    def test_toml_loops_disabled_prevents_ownership_claim(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _mark_active("mid-sess-disabled")
        monkeypatch.setattr(router, "_tick_meta_stale", lambda: True)
        monkeypatch.setattr(router, "_session_has_loop", lambda sid: False)

        toml_path = tmp_path / ".teatree.toml"
        toml_path.write_text("[loops]\nenabled = false\n", encoding="utf-8")
        monkeypatch.setenv("HOME", str(tmp_path))

        handle_enforce_loop_on_prompt({"session_id": "mid-sess-disabled"})

        assert _read_loop_registry() == {}

    def test_env_loops_disabled_all_prevents_ownership_claim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mark_active("mid-sess-env-disabled")
        monkeypatch.setattr(router, "_tick_meta_stale", lambda: True)
        monkeypatch.setattr(router, "_session_has_loop", lambda sid: False)
        monkeypatch.setenv("T3_LOOPS_DISABLED", "all")

        handle_enforce_loop_on_prompt({"session_id": "mid-sess-env-disabled"})

        assert _read_loop_registry() == {}

    def test_loop_disown_prevents_ownership_claim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mark_active("mid-sess-disown")
        monkeypatch.setattr(router, "_tick_meta_stale", lambda: True)
        monkeypatch.setattr(router, "_session_has_loop", lambda sid: False)
        monkeypatch.setenv("T3_LOOP_DISOWN", "1")

        handle_enforce_loop_on_prompt({"session_id": "mid-sess-disown"})

        assert _read_loop_registry() == {}

    def test_fresh_session_without_marker_does_not_claim_from_prompt(self) -> None:
        handle_enforce_loop_on_prompt({"session_id": "fresh-mid"})
        assert _read_loop_registry() == {}


# ── Statusline shell script gating ────────────────────────────────────

_BASH = shutil.which("bash") or "/bin/bash"


class TestStatuslineGating:
    def _run_statusline(
        self,
        session_id: str,
        state_dir: Path,
        *,
        extra_env: dict | None = None,
    ) -> str:
        script = Path(__file__).resolve().parents[1] / "hooks" / "scripts" / "statusline.sh"
        payload = json.dumps({"session_id": session_id})
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "TEATREE_CLAUDE_STATUSLINE_STATE_DIR": str(state_dir),
            "TEATREE_STATUSLINE_FILE": str(state_dir / "statusline.txt"),
        }
        if extra_env:
            env.update(extra_env)
        result = subprocess.run(
            [_BASH, str(script)],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        return result.stdout

    def test_no_marker_produces_no_output(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        out = self._run_statusline("no-teatree-sess", state_dir)
        assert out == ""

    def test_marker_present_produces_output(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "teatree-sess.teatree-active").touch()
        out = self._run_statusline("teatree-sess", state_dir)
        assert out != ""
