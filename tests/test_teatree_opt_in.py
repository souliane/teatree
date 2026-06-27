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
import sys
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import (
    _OWNER_LOOP,
    _loop_auto_load_active,
    _loop_registration_exempt,
    _read_loop_registry,
    _t3_engaged,
    _teatree_active,
    _teatree_engaged,
    _write_loop_registry,
    handle_enforce_loop_on_prompt,
    handle_enforce_skill_loading,
    handle_session_start_bootstrap,
    handle_track_skill_usage,
    handle_user_prompt_submit,
)
from hooks.scripts.teatree_settings import autoload_enabled

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import skill_loader as skill_loader_mod  # noqa: E402

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
    # Hermetic HOME: ``_autoload_enabled`` reads ``~/.teatree.toml``; a clean home
    # keeps the default-OFF (#256) path deterministic regardless of the
    # developer's own config.
    clean_home = tmp_path / "home"
    clean_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(clean_home))
    # ``autoload`` is the ONE owner flag — it both engages the session AND arms
    # its loops (the former separate ``[loops] auto_load`` arming flag is
    # subsumed). Model the opted-in loop OWNER for the marker-mechanism tests
    # below by setting ``T3_AUTOLOAD``; classes that exercise the default-OFF
    # engagement / arming path delete this var in their own fixtures.
    monkeypatch.setenv("T3_AUTOLOAD", "1")


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
    def test_fresh_session_without_marker_emits_how_to_advisory(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #256 default-OFF: a fresh, not-engaged session (autoload OFF) no longer
        # returns silently — it surfaces the one-line how-to-start advisory and
        # never the loop bootstrap directive.
        monkeypatch.delenv("T3_AUTOLOAD", raising=False)
        handle_session_start_bootstrap({"session_id": "no-teatree"})
        out = capsys.readouterr().out
        assert out != ""
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "run /teatree" in ctx
        assert "t3 loop tick" not in ctx

    def test_fresh_session_without_marker_does_not_claim_ownership(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_AUTOLOAD", raising=False)
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
        # #2650: the owner now registers ONE `/loop` per enabled DB Loop (the seam
        # is patched here to two specs so this stays a DB-free gating test).
        from hooks.scripts import loop_registrations  # noqa: PLC0415

        class _Spec:
            def __init__(self, slot_id: str) -> None:
                self.slot_id = slot_id
                self.cron = "*/5 * * * *"
                self.prompt = f"Run `t3 loops tick --loop {slot_id}` in Bash, then briefly report the tick summary."

        _mark_active("teatree-session")
        monkeypatch.setattr(router, "_tick_meta_stale", lambda: True)
        monkeypatch.setattr(router, "_session_has_loop", lambda sid: False)
        monkeypatch.setattr(loop_registrations, "_enabled_loop_specs", lambda: [_Spec("inbox"), _Spec("ship")])
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

    def test_toml_loops_disabled_no_longer_prevents_ownership_claim(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The ``[loops] enabled = false`` toml kill-switch is removed — there is
        # no ``[loops]`` toml disabled-state tier. Writing it is INERT and no
        # longer prunes the ownership claim; loop pause/disable lives in the DB
        # ``LoopState`` tier, and the in-process ``T3_LOOP_DISOWN`` knob is the
        # orthogonal mitigation (test_loop_disown_prevents_ownership_claim).
        _mark_active("mid-sess-disabled")
        monkeypatch.setattr(router, "_tick_meta_stale", lambda: True)
        monkeypatch.setattr(router, "_session_has_loop", lambda sid: False)

        toml_path = tmp_path / ".teatree.toml"
        toml_path.write_text("[loops]\nenabled = false\n", encoding="utf-8")
        monkeypatch.setenv("HOME", str(tmp_path))

        handle_enforce_loop_on_prompt({"session_id": "mid-sess-disabled"})

        owner = _read_loop_registry().get(_OWNER_LOOP)
        assert owner is not None
        assert owner["session_id"] == "mid-sess-disabled"

    def test_env_loops_disabled_all_no_longer_prevents_ownership_claim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # ``T3_LOOPS_DISABLED`` is removed — it is INERT and no longer prunes the
        # ownership claim. Loop pause/disable lives in the DB ``LoopState`` tier;
        # the in-process ``T3_LOOP_DISOWN`` knob is the orthogonal mitigation
        # (test_loop_disown_prevents_ownership_claim).
        _mark_active("mid-sess-env-disabled")
        monkeypatch.setattr(router, "_tick_meta_stale", lambda: True)
        monkeypatch.setattr(router, "_session_has_loop", lambda sid: False)
        monkeypatch.setenv("T3_LOOPS_DISABLED", "all")

        handle_enforce_loop_on_prompt({"session_id": "mid-sess-env-disabled"})

        owner = _read_loop_registry().get(_OWNER_LOOP)
        assert owner is not None
        assert owner["session_id"] == "mid-sess-env-disabled"

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


# ── #256: session-start auto-load is opt-in (default OFF, colleague-friendly) ──


class TestLoopAutoLoadOptInGate:
    """A teatree-marked session that did NOT enable autoload is silent (#256).

    Symmetric must-fire/must-NOT-fire for ``_loop_auto_load_active`` and each of
    the three injection points it now gates (bootstrap claim, prompt-time cron
    nag, registration nudge exemption). The marker is always present here, so the
    ONLY variable is the ``autoload`` opt-in — revert the ``_loop_auto_load_active``
    gate at any call site and the matching ``*_silent`` assertion goes RED.
    """

    @pytest.fixture(autouse=True)
    def _marked_colleague(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mark_active("colleague")
        # Drop the file-level autoload opt-in so the default-OFF path is exercised.
        monkeypatch.delenv("T3_AUTOLOAD", raising=False)

    def _opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_AUTOLOAD", "1")

    # combined predicate ───────────────────────────────────────────────
    def test_active_predicate_off_without_opt_in(self) -> None:
        assert _loop_auto_load_active("colleague") is False

    def test_active_predicate_on_with_opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._opt_in(monkeypatch)
        assert _loop_auto_load_active("colleague") is True

    # bootstrap (SessionStart) ─────────────────────────────────────────
    def test_bootstrap_silent_without_opt_in(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_session_start_bootstrap({"session_id": "colleague"})
        assert capsys.readouterr().out == ""

    def test_bootstrap_does_not_claim_ownership_without_opt_in(self) -> None:
        handle_session_start_bootstrap({"session_id": "colleague"})
        assert _read_loop_registry() == {}

    def test_bootstrap_fires_with_opt_in(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._opt_in(monkeypatch)
        handle_session_start_bootstrap({"session_id": "colleague"})
        out = capsys.readouterr().out
        assert "t3 loop tick" in out
        assert _read_loop_registry().get(_OWNER_LOOP, {}).get("session_id") == "colleague"

    # prompt-time cron nag ─────────────────────────────────────────────
    def test_prompt_nag_silent_without_opt_in(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(router, "_tick_meta_stale", lambda: True)
        monkeypatch.setattr(router, "_session_has_loop", lambda sid: False)
        handle_enforce_loop_on_prompt({"session_id": "colleague"})
        assert capsys.readouterr().out == ""
        assert _read_loop_registry() == {}

    def test_prompt_nag_fires_with_opt_in(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hooks.scripts import loop_registrations  # noqa: PLC0415

        class _Spec:
            slot_id = "t3-loop-inbox"
            cron = "*/1 * * * *"
            prompt = "Run `t3 loops tick --loop inbox` in Bash, then briefly report the tick summary."

        self._opt_in(monkeypatch)
        monkeypatch.setattr(router, "_tick_meta_stale", lambda: True)
        monkeypatch.setattr(router, "_session_has_loop", lambda sid: False)
        monkeypatch.setattr(loop_registrations, "_enabled_loop_specs", lambda: [_Spec()])
        handle_enforce_loop_on_prompt({"session_id": "colleague"})
        assert "register_cron" in capsys.readouterr().out

    # registration nudge exemption ─────────────────────────────────────
    def test_registration_exempt_without_opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(router, "_loop_registration_gate_enabled", lambda: True)
        monkeypatch.setattr(router, "_session_drives_loop", lambda sid: True)
        assert _loop_registration_exempt({"session_id": "colleague", "tool_name": "Bash"}) is True

    def test_registration_not_exempt_with_opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._opt_in(monkeypatch)
        monkeypatch.setattr(router, "_loop_registration_gate_enabled", lambda: True)
        monkeypatch.setattr(router, "_session_drives_loop", lambda sid: True)
        assert _loop_registration_exempt({"session_id": "colleague", "tool_name": "Bash"}) is False


# ── Statusline shell script gating ────────────────────────────────────

_BASH = shutil.which("bash") or "/bin/bash"


class TestStatuslineGating:
    def _run_statusline(
        self,
        session_id: str,
        state_dir: Path,
        *,
        home: Path | None = None,
        extra_env: dict | None = None,
    ) -> str:
        script = Path(__file__).resolve().parents[1] / "hooks" / "scripts" / "statusline.sh"
        payload = json.dumps({"session_id": session_id})
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(home) if home is not None else os.environ.get("HOME", ""),
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
        out = self._run_statusline("no-teatree-sess", state_dir, extra_env={"T3_AUTOLOAD": "1"})
        assert out == ""

    def test_marker_present_but_auto_load_off_produces_no_output(self, tmp_path: Path) -> None:
        # The #256 colleague case: a session that loaded teatree (marker
        # present) but never enabled autoload stays silent.
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "teatree-sess.teatree-active").touch()
        out = self._run_statusline("teatree-sess", state_dir, home=tmp_path / "fresh-home")
        assert out == ""

    def test_marker_and_env_opt_in_produces_output(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "teatree-sess.teatree-active").touch()
        out = self._run_statusline("teatree-sess", state_dir, extra_env={"T3_AUTOLOAD": "1"})
        assert out != ""

    def test_marker_and_toml_opt_in_produces_output(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "teatree-sess.teatree-active").touch()
        home = tmp_path / "opted-in-home"
        home.mkdir(parents=True, exist_ok=True)
        (home / ".teatree.toml").write_text("[teatree]\nautoload = true\n", encoding="utf-8")
        out = self._run_statusline("teatree-sess", state_dir, home=home)
        assert out != ""


# ── #256: default-off teatree autoload + engagement seam ──────────────────


class TestAutoloadEnabledHelper:
    """``autoload_enabled`` — env-first, then ``[teatree] autoload``, default OFF, fail-closed."""

    @pytest.fixture(autouse=True)
    def _no_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_AUTOLOAD", raising=False)

    def test_default_off_with_no_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path / "no-config-home"))
        assert autoload_enabled() is False

    def test_env_truthy_enables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_AUTOLOAD", "1")
        assert autoload_enabled() is True

    def test_env_falsey_disables_over_toml_true(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = tmp_path / "h"
        home.mkdir()
        (home / ".teatree.toml").write_text("[teatree]\nautoload = true\n", encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("T3_AUTOLOAD", "false")
        assert autoload_enabled() is False

    def test_toml_true_enables(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = tmp_path / "h"
        home.mkdir()
        (home / ".teatree.toml").write_text("[teatree]\nautoload = true\n", encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        assert autoload_enabled() is True

    def test_broken_config_fails_closed_off(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = tmp_path / "h"
        home.mkdir()
        (home / ".teatree.toml").write_text("not = = valid toml\n", encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        assert autoload_enabled() is False

    def test_quoted_string_true_is_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A quoted "true" (a string, not a bare bool) must not enable autoload.
        home = tmp_path / "h"
        home.mkdir()
        (home / ".teatree.toml").write_text('[teatree]\nautoload = "true"\n', encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        assert autoload_enabled() is False


class TestTeatreeEngagedSeam:
    """``_teatree_engaged`` = autoload OR ``.teatree-active`` OR ``.t3-engaged`` (#256)."""

    @pytest.fixture(autouse=True)
    def _no_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The file fixture models the opted-in owner via T3_AUTOLOAD; drop it so
        # the default-OFF engagement seam is exercised here.
        monkeypatch.delenv("T3_AUTOLOAD", raising=False)

    def test_not_engaged_by_default(self) -> None:
        assert _teatree_engaged("fresh") is False

    def test_engaged_via_autoload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_AUTOLOAD", "1")
        assert _teatree_engaged("fresh") is True

    def test_engaged_via_teatree_active_marker(self) -> None:
        _mark_active("tt-eng")
        assert _teatree_engaged("tt-eng") is True

    def test_engaged_via_t3_engaged_marker(self) -> None:
        (router.STATE_DIR / "marker-sess.t3-engaged").touch()
        assert _t3_engaged("marker-sess") is True
        assert _teatree_engaged("marker-sess") is True

    def test_empty_session_id_is_not_engaged(self) -> None:
        assert _t3_engaged("") is False
        assert _teatree_engaged("") is False


class TestAutoloadSessionStart:
    """#256 (a): autoload ON flips ``.teatree-active`` + fires the loop bootstrap, not the how-to."""

    @pytest.fixture(autouse=True)
    def _no_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Drop the file fixture's autoload opt-in; the on-tests re-enable it.
        monkeypatch.delenv("T3_AUTOLOAD", raising=False)

    def test_autoload_on_touches_teatree_active_and_emits_tick_directive(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # autoload is the single owner flag: it flips ``.teatree-active`` AND arms
        # the loop bootstrap, so the existing loop bootstrap fires unchanged.
        monkeypatch.setenv("T3_AUTOLOAD", "1")
        handle_session_start_bootstrap({"session_id": "owner-default"})
        assert _is_marked_active("owner-default")
        ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        assert "t3 loop tick" in ctx
        assert "run /teatree" not in ctx

    def test_autoload_on_claims_ownership(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_AUTOLOAD", "1")
        handle_session_start_bootstrap({"session_id": "owner-default"})
        assert _read_loop_registry().get(_OWNER_LOOP, {}).get("session_id") == "owner-default"

    def test_default_off_emits_how_to_and_does_not_claim(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_session_start_bootstrap({"session_id": "off-sess"})
        ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        assert "run /teatree" in ctx
        assert "autoload = true" in ctx
        assert "t3 loop tick" not in ctx
        assert _read_loop_registry() == {}

    def test_compact_resume_default_off_skips_how_to(self, capsys: pytest.CaptureFixture[str]) -> None:
        # On a compact/resume of a not-engaged session the how-to is suppressed,
        # but the merge still runs so snapshot-recovery / hand-off is never dropped.
        handle_session_start_bootstrap({"session_id": "off-compact", "source": "compact"})
        assert "run /teatree" not in capsys.readouterr().out


class TestDefaultOffUserPromptSubmit:
    """#256: UserPromptSubmit suppresses the suggester + reminder + .pending write until engaged."""

    @pytest.fixture(autouse=True)
    def _no_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Drop the file fixture's autoload opt-in; the engaged tests re-enable it.
        monkeypatch.delenv("T3_AUTOLOAD", raising=False)

    @pytest.fixture(autouse=True)
    def suggester_calls(self, monkeypatch: pytest.MonkeyPatch) -> list:
        calls: list = []

        def _stub(loader_input: dict) -> dict:
            calls.append(loader_input)
            return {"suggestions": ["code"], "advisory": [], "intent": "code"}

        monkeypatch.setattr(skill_loader_mod, "suggest_skills", _stub)
        return calls

    def _pending(self, session_id: str) -> str | None:
        path = router.STATE_DIR / f"{session_id}.pending"
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def test_default_off_writes_empty_pending_prints_nothing_and_skips_suggester(
        self, capsys: pytest.CaptureFixture[str], suggester_calls: list
    ) -> None:
        handle_user_prompt_submit({"session_id": "ups-off", "prompt": "fix the bug in foo.py and run ruff"})
        assert capsys.readouterr().out == ""
        # Empty .pending → the PreToolUse skill-loading gate never blocks (never-lockout).
        assert self._pending("ups-off") == ""
        # Anti-vacuous: the suggester that WOULD have produced output was never called.
        assert suggester_calls == []

    def test_engaged_via_autoload_runs_suggester(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, suggester_calls: list
    ) -> None:
        monkeypatch.setenv("T3_AUTOLOAD", "1")
        handle_user_prompt_submit({"session_id": "ups-on", "prompt": "fix the bug"})
        out = capsys.readouterr().out
        assert "LOAD THESE SKILLS NOW" in out
        assert suggester_calls != []

    def test_engaged_via_t3_marker_runs_suggester(
        self, capsys: pytest.CaptureFixture[str], suggester_calls: list
    ) -> None:
        (router.STATE_DIR / "ups-t3.t3-engaged").touch()
        handle_user_prompt_submit({"session_id": "ups-t3", "prompt": "fix the bug"})
        assert "LOAD THESE SKILLS NOW" in capsys.readouterr().out
        assert suggester_calls != []


class TestOption1T3EngagedMarker:
    """#256 Option-1: any ``t3:`` skill engages the SUGGESTER (``.t3-engaged``); loops stay off."""

    @pytest.fixture(autouse=True)
    def _identity_closure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(router, "_resolve_skill_closure", lambda skills: skills)
        # Drop the file fixture's autoload opt-in so engagement is driven only by
        # the skill markers under test (not by autoload).
        monkeypatch.delenv("T3_AUTOLOAD", raising=False)

    def test_t3_code_sets_t3_engaged_not_teatree_active(self) -> None:
        handle_track_skill_usage({"session_id": "o1", "tool_name": "Skill", "tool_input": {"skill": "t3:code"}})
        assert (router.STATE_DIR / "o1.t3-engaged").is_file()
        # Loops are reserved for teatree-requiring skills — never armed by a plain
        # lifecycle skill (keeps TestRealClosureMarkerActivation's contract).
        assert not _is_marked_active("o1")

    def test_t3_code_engages_session_but_loops_stay_off(self) -> None:
        handle_track_skill_usage({"session_id": "o1b", "tool_name": "Skill", "tool_input": {"skill": "t3:code"}})
        assert _teatree_engaged("o1b") is True
        assert _loop_auto_load_active("o1b") is False

    def test_instructions_loaded_t3_skill_sets_t3_engaged(self) -> None:
        handle_track_skill_usage({"session_id": "o1c", "skills": [{"name": "t3:review"}]})
        assert (router.STATE_DIR / "o1c.t3-engaged").is_file()
        assert not _is_marked_active("o1c")


class TestExplicitTeatreeEngages:
    """#256: explicitly loading ``/teatree`` while OFF sets the marker and engages."""

    @pytest.fixture(autouse=True)
    def _identity_closure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(router, "_resolve_skill_closure", lambda skills: skills)
        # Drop the file fixture's autoload opt-in so engagement is driven only by
        # the skill markers under test (not by autoload).
        monkeypatch.delenv("T3_AUTOLOAD", raising=False)

    def test_teatree_skill_engages_session(self) -> None:
        assert _teatree_engaged("tt-explicit") is False
        handle_track_skill_usage(
            {"session_id": "tt-explicit", "tool_name": "Skill", "tool_input": {"skill": "t3:teatree"}}
        )
        assert _is_marked_active("tt-explicit")
        assert _teatree_engaged("tt-explicit") is True


class TestDefaultOffNeverLockout:
    """#256: a default-off, not-engaged session never hard-blocks a .py Edit or a Bash command."""

    @pytest.fixture(autouse=True)
    def _no_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Drop the file fixture's autoload opt-in so the session stays not-engaged
        # (the empty .pending is what keeps the gate from ever hard-blocking).
        monkeypatch.delenv("T3_AUTOLOAD", raising=False)

    def test_py_edit_not_blocked_after_default_off_prompt(self, tmp_path: Path) -> None:
        handle_user_prompt_submit({"session_id": "ll-edit", "prompt": "fix the bug in foo.py and run ruff"})
        blocked = handle_enforce_skill_loading(
            {"session_id": "ll-edit", "tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / "work" / "x.py")}}
        )
        assert blocked is False

    def test_bash_not_blocked_after_default_off_prompt(self) -> None:
        handle_user_prompt_submit({"session_id": "ll-bash", "prompt": "run the test suite please"})
        blocked = handle_enforce_skill_loading(
            {"session_id": "ll-bash", "tool_name": "Bash", "tool_input": {"command": "uv run pytest -q"}}
        )
        assert blocked is False


class TestMaybeEngageT3Normalization:
    """Nit A: ``_maybe_engage_t3`` canonicalizes the token through the SAME identity seam.

    The bug: ``_maybe_engage_t3`` engaged on a raw ``name.startswith("t3:")`` while
    its sibling ``_skill_load_activates_teatree`` normalized every token through the
    ``normalize_skill_name`` identity seam — so a bare / path-form ``t3:`` skill was
    detected by one but not the other. The fix routes ``_maybe_engage_t3`` through
    ``normalize_skill_name`` (normalize UP), so bare and qualified forms engage
    identically while a foreign namespace keeps its qualifier and never matches.
    """

    @pytest.fixture(autouse=True)
    def _snapshot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Deterministic owned-set + namespace so the seam resolves without a live
        # skill-tree scan: ``code``/``teatree``/``review`` are owned in ``t3``.
        monkeypatch.setattr(router, "_skill_canon_snapshot", lambda: (frozenset({"code", "teatree", "review"}), "t3"))

    @staticmethod
    def _engaged(session_id: str) -> bool:
        return (router.STATE_DIR / f"{session_id}.t3-engaged").is_file()

    def test_qualified_t3_token_engages(self) -> None:
        router._maybe_engage_t3("q", ["t3:code"])
        assert self._engaged("q")

    def test_bare_owned_token_engages_like_qualified(self) -> None:
        # RED before the fix: ``"code".startswith("t3:")`` is False.
        router._maybe_engage_t3("b", ["code"])
        assert self._engaged("b")

    def test_path_form_token_engages(self) -> None:
        # RED before the fix: the ``/SKILL.md`` path form does not start with ``t3:``.
        router._maybe_engage_t3("p", ["teatree/SKILL.md"])
        assert self._engaged("p")

    def test_foreign_namespace_does_not_engage(self) -> None:
        # Identity-correct: a foreign-namespaced token keeps its qualifier and is
        # never promoted to ``t3:`` (no qualifier-stripping conflation).
        router._maybe_engage_t3("f", ["other:review"])
        assert not self._engaged("f")

    def test_non_owned_skill_does_not_engage(self) -> None:
        router._maybe_engage_t3("a", ["ac-python"])
        assert not self._engaged("a")
