"""Repeated-denial circuit breaker — stop runaway loops burning tokens.

A stuck session can hit the SAME gate denial over and over: a real session hit
one skill-loading denial 16 times consecutively across ~683 model turns, burning
~2M output / ~190M total tokens. Retrying never satisfies a false or
unsatisfiable demand, so the model retries forever. The circuit breaker trips at
the K-th CONSECUTIVE identical denial, tiered by gate class. A UX / non-safety
gate (allow-list, the skill-loading gate) FAILS OPEN the K-th call to break the
loop and records a durable ``loop_circuit_broken`` signal. A SAFETY gate
(everything else) NEVER auto-relaxes — it keeps denying but escalates the reason
so the model stops retrying.

Integration-leaning: every test drives the REAL deny chokepoint by invoking
``hook_router.main()`` as a subprocess (the real PreToolUse handler chain, real
``STATE_DIR`` on ``tmp_path``, real fixture skill tree, real streak counter — the
counter is never mocked). The deny-streak and circuit-broken signal are read back
from their real per-session state files.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK_ROUTER = Path(__file__).resolve().parent.parent / "hooks" / "scripts" / "hook_router.py"


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    """A subprocess env with STATE_DIR, skill search dirs, and HOME on tmp_path.

    Seeds one real, loadable skill (``ac-reviewing-codebase``) so the
    skill-loading gate has a genuine load-first demand to enforce, and points
    ``HOME`` at a clean temp dir so the breaker's ``~/.teatree.toml`` read sees
    its default (enabled) unless a test writes a config.
    """
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    skills = tmp_path / "skills"
    skill = skills / "ac-reviewing-codebase"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text("---\nname: ac-reviewing-codebase\n---\n", encoding="utf-8")

    return {
        **os.environ,
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "TEATREE_CLAUDE_STATUSLINE_STATE_DIR": str(state),
        "T3_SKILL_SEARCH_DIRS": str(skills),
    }


def _state_dir(env: dict[str, str]) -> Path:
    return Path(env["TEATREE_CLAUDE_STATUSLINE_STATE_DIR"])


def _run(env: dict[str, str], payload: dict) -> tuple[int, dict | None, str]:
    """Drive the real PreToolUse chain via ``main()``; return (rc, deny-json, stderr)."""
    result = subprocess.run(
        [sys.executable, str(HOOK_ROUTER), "--event", "PreToolUse"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
        env=env,
    )
    payload_out = json.loads(result.stdout) if result.stdout.strip() else None
    return result.returncode, payload_out, result.stderr


def _streak(env: dict[str, str], session_id: str) -> dict | None:
    path = _state_dir(env) / f"{session_id}.deny-streak"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _circuit_broken(env: dict[str, str], session_id: str) -> list[str]:
    path = _state_dir(env) / f"{session_id}.circuit-broken"
    if not path.is_file():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


def _seed_pending(env: dict[str, str], session_id: str, skills: list[str]) -> None:
    (_state_dir(env) / f"{session_id}.pending").write_text("\n".join(skills) + "\n", encoding="utf-8")


def _skill_deny(session_id: str) -> dict:
    """An Edit of a Python file that trips ONLY the skill-loading (UX) gate when pending is seeded.

    The skill-loading gate is scoped to genuine code work, so the call must
    touch a ``.py`` file; an ``Edit`` keeps it clear of the Bash-only
    orchestrator-boundary safety gate the other helpers exercise.
    """
    return {"session_id": session_id, "tool_name": "Edit", "tool_input": {"file_path": "src/teatree/core/probe.py"}}


def _safety_deny(session_id: str) -> dict:
    """A main-agent heavy-Bash call that trips the orchestrator-boundary SAFETY gate."""
    return {"session_id": session_id, "tool_name": "Bash", "tool_input": {"command": "uv run pytest --no-cov -q"}}


def _assert_denied(rc: int, payload: dict | None) -> None:
    assert rc == 2
    assert payload is not None
    assert payload["permissionDecision"] == "deny"


def _assert_streak_count(env: dict[str, str], session_id: str, expected: int) -> None:
    streak = _streak(env, session_id)
    assert streak is not None
    assert streak["count"] == expected


class TestUxGateTripsOpenAtThreshold:
    """The skill-loading (UX) gate auto-relaxes at the 3rd consecutive denial.

    First two calls deny; the third fails open (allow) and records a durable
    ``loop_circuit_broken`` signal. RED on pre-change code: the 3rd still denies.
    """

    def test_third_consecutive_identical_skill_deny_fails_open_with_signal(self, env: dict[str, str]) -> None:
        _seed_pending(env, "ux", ["ac-reviewing-codebase"])

        rc1, payload1, _ = _run(env, _skill_deny("ux"))
        rc2, payload2, _ = _run(env, _skill_deny("ux"))
        _assert_denied(rc1, payload1)
        _assert_denied(rc2, payload2)
        _assert_streak_count(env, "ux", 2)

        rc3, payload3, stderr3 = _run(env, _skill_deny("ux"))
        assert rc3 == 0, "3rd identical UX denial must FAIL OPEN (allow) to break the loop"
        assert payload3 is None, "no deny payload on the auto-relaxed call"
        assert "CIRCUIT BREAKER" in stderr3

        signal = _circuit_broken(env, "ux")
        assert len(signal) == 1, "exactly one loop_circuit_broken signal recorded"
        assert "skill-loading-enforcement" in signal[0]

    def test_auto_relax_resets_the_streak(self, env: dict[str, str]) -> None:
        _seed_pending(env, "ux2", ["ac-reviewing-codebase"])
        rcs = [_run(env, _skill_deny("ux2"))[0] for _ in range(3)]
        assert rcs == [2, 2, 0], "first two deny, the third fails open"
        assert _streak(env, "ux2") is None, "the breaker resets the streak after relaxing the UX gate"


class TestSafetyGateNeverOpens:
    """A SAFETY gate keeps denying past the threshold; it never auto-relaxes."""

    def test_third_consecutive_safety_deny_still_denies_with_escalation(self, env: dict[str, str]) -> None:
        rc1, _, _ = _run(env, _safety_deny("safe"))
        rc2, _, _ = _run(env, _safety_deny("safe"))
        assert rc1 == 2
        assert rc2 == 2

        rc3, payload3, stderr3 = _run(env, _safety_deny("safe"))
        assert rc3 == 2, "a safety gate must NEVER fail open"
        assert payload3 is not None
        assert payload3["permissionDecision"] == "deny"
        assert "CIRCUIT BREAKER" in payload3["permissionDecisionReason"]
        assert "LOOPING" in payload3["permissionDecisionReason"]
        assert "CIRCUIT BREAKER" in stderr3

        signal = _circuit_broken(env, "safe")
        assert len(signal) == 1, "the safety-gate loop is recorded as a loop_circuit_broken signal"

    def test_safety_signal_recorded_once_not_per_subsequent_deny(self, env: dict[str, str]) -> None:
        for _ in range(5):
            _run(env, _safety_deny("safe-dedup"))
        assert len(_circuit_broken(env, "safe-dedup")) == 1, "the signal is deduped by fingerprint"


class TestAllowResetsStreak:
    """An ALLOWED call between denials resets the streak, so the breaker never trips."""

    def test_allow_between_denials_prevents_trip(self, env: dict[str, str]) -> None:
        _seed_pending(env, "reset", ["ac-reviewing-codebase"])
        allowed = {
            "session_id": "reset",
            "tool_name": "Bash",
            "tool_input": {"command": "git status # [skill-load-ok: genuine progress]"},
        }

        rc1, _, _ = _run(env, _skill_deny("reset"))
        rc2, _, _ = _run(env, _skill_deny("reset"))
        rc_allow, payload_allow, _ = _run(env, allowed)
        rc3, _, _ = _run(env, _skill_deny("reset"))
        rc4, _, _ = _run(env, _skill_deny("reset"))

        assert [rc1, rc2] == [2, 2]
        assert rc_allow == 0, "the escaped call is allowed (genuine progress)"
        assert payload_allow is None
        assert [rc3, rc4] == [2, 2], "denials after the allow restart the count — never reach the threshold"
        _assert_streak_count(env, "reset", 2)
        assert _circuit_broken(env, "reset") == [], "the breaker never tripped"


class TestKillSwitchIsPureNoOp:
    """``deny_circuit_breaker_enabled = false`` makes the breaker a pure pass-through."""

    def test_disabled_breaker_passes_denials_through_unchanged(self, env: dict[str, str]) -> None:
        (Path(env["HOME"]) / ".teatree.toml").write_text(
            "[teatree]\ndeny_circuit_breaker_enabled = false\n", encoding="utf-8"
        )
        for _ in range(5):
            rc, payload, _ = _run(env, _safety_deny("off"))
            _assert_denied(rc, payload)
            assert payload is not None
            assert "CIRCUIT BREAKER" not in payload["permissionDecisionReason"]

        assert _streak(env, "off") is None, "the breaker writes no streak state when disabled"
        assert _circuit_broken(env, "off") == [], "the breaker records no signal when disabled"


class TestFingerprintDiscrimination:
    """Two DIFFERENT denials interleaved do not accumulate toward a trip."""

    def test_interleaved_distinct_safety_denials_never_trip(self, env: dict[str, str]) -> None:
        heavy = _safety_deny("fp")
        no_verify = {
            "session_id": "fp",
            "tool_name": "Bash",
            "tool_input": {"command": "git commit --no-verify -m x"},
        }
        for payload in (heavy, no_verify, heavy, no_verify, heavy, no_verify):
            rc, _, _ = _run(env, payload)
            assert rc == 2
            # Each distinct denial resets the other's streak, so neither ever
            # climbs past 1.
            _assert_streak_count(env, "fp", 1)

        assert _circuit_broken(env, "fp") == [], "distinct denials never trip the breaker"
