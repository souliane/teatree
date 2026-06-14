"""Tests for ``teatree.core.safe_kill`` (#2225).

The guard refuses to signal a process unless BOTH hold. Positive identity —
the pid maps to a KNOWN dead/failed target by session id, never by a heuristic
"looks idle". Confirmed non-live — two CPU samples show no activity, STAT is
not a running/foreground state (``R``/``R+``), and a hang cause is stated.

Only the two externality boundaries are mocked: the per-pid liveness sample
(real impl shells out to ``ps``) and the pid→identity resolution (real impl
reads ``~/.claude/sessions`` + the Task ORM). The guard logic itself is never
mocked, so the must-refuse cases go red if the guard is bypassed.
"""

import pytest

from teatree.core.safe_kill import Liveness, SafeKillError, TargetIdentity, evaluate_safe_kill, safe_kill

_DEAD_LIVENESS = Liveness(stat="Z", cpu_sample_1=0.0, cpu_sample_2=0.0, output_advanced=False)
_RUNNING_LIVENESS = Liveness(stat="R+", cpu_sample_1=12.4, cpu_sample_2=9.1, output_advanced=True)
_IDLE_FOREGROUND_LIVENESS = Liveness(stat="S+", cpu_sample_1=0.0, cpu_sample_2=0.0, output_advanced=False)

_FAILED_TARGET = TargetIdentity(session_id="sess-dead", task_id=7, is_dead_target=True)
_UNKNOWN_TARGET = TargetIdentity(session_id="", task_id=None, is_dead_target=False)
_LIVE_TASK_TARGET = TargetIdentity(session_id="sess-live", task_id=9, is_dead_target=False)


def _resolver(identity: TargetIdentity):
    return lambda _pid: identity


def _sampler(liveness: Liveness):
    return lambda _pid: liveness


class TestPositiveIdentity:
    def test_refuses_when_pid_maps_to_no_known_dead_task(self) -> None:
        verdict = evaluate_safe_kill(
            4242,
            hang_cause="no response for 20 min",
            resolve_identity=_resolver(_UNKNOWN_TARGET),
            sample_liveness=_sampler(_DEAD_LIVENESS),
        )
        assert not verdict.allowed
        assert "no known dead" in verdict.reason.lower()

    def test_refuses_when_pid_maps_to_a_still_claimed_task(self) -> None:
        verdict = evaluate_safe_kill(
            4242,
            hang_cause="seems stuck",
            resolve_identity=_resolver(_LIVE_TASK_TARGET),
            sample_liveness=_sampler(_DEAD_LIVENESS),
        )
        assert not verdict.allowed
        assert "sess-live" in verdict.reason

    def test_allows_dead_pid_mapped_to_a_failed_task(self) -> None:
        verdict = evaluate_safe_kill(
            4242,
            hang_cause="agent crashed mid-attempt, no output since",
            resolve_identity=_resolver(_FAILED_TARGET),
            sample_liveness=_sampler(_DEAD_LIVENESS),
        )
        assert verdict.allowed
        assert verdict.reason == ""


class TestConfirmedNonLive:
    def test_refuses_running_process_outright(self) -> None:
        verdict = evaluate_safe_kill(
            4242,
            hang_cause="high uptime",
            resolve_identity=_resolver(_FAILED_TARGET),
            sample_liveness=_sampler(_RUNNING_LIVENESS),
        )
        assert not verdict.allowed
        assert "R+" in verdict.reason

    def test_refuses_foreground_session_with_no_cpu_but_live_tty(self) -> None:
        verdict = evaluate_safe_kill(
            4242,
            hang_cause="uptime looks high",
            resolve_identity=_resolver(_FAILED_TARGET),
            sample_liveness=_sampler(_IDLE_FOREGROUND_LIVENESS),
        )
        assert not verdict.allowed
        assert "S+" in verdict.reason

    def test_refuses_when_cpu_still_active_between_samples(self) -> None:
        active = Liveness(stat="S", cpu_sample_1=0.0, cpu_sample_2=4.0, output_advanced=False)
        verdict = evaluate_safe_kill(
            4242,
            hang_cause="stalled",
            resolve_identity=_resolver(_FAILED_TARGET),
            sample_liveness=_sampler(active),
        )
        assert not verdict.allowed
        assert "cpu" in verdict.reason.lower()

    def test_refuses_when_output_still_advancing(self) -> None:
        advancing = Liveness(stat="S", cpu_sample_1=0.0, cpu_sample_2=0.0, output_advanced=True)
        verdict = evaluate_safe_kill(
            4242,
            hang_cause="stalled",
            resolve_identity=_resolver(_FAILED_TARGET),
            sample_liveness=_sampler(advancing),
        )
        assert not verdict.allowed
        assert "output" in verdict.reason.lower()


class TestHangCauseRequired:
    def test_refuses_when_no_hang_cause_stated(self) -> None:
        verdict = evaluate_safe_kill(
            4242,
            hang_cause="   ",
            resolve_identity=_resolver(_FAILED_TARGET),
            sample_liveness=_sampler(_DEAD_LIVENESS),
        )
        assert not verdict.allowed
        assert "hang cause" in verdict.reason.lower()


class TestRefusalEvidence:
    def test_refusal_names_session_id_and_liveness_and_confirm_with_user(self) -> None:
        verdict = evaluate_safe_kill(
            4242,
            hang_cause="high uptime",
            resolve_identity=_resolver(TargetIdentity(session_id="sess-xyz", task_id=3, is_dead_target=True)),
            sample_liveness=_sampler(_RUNNING_LIVENESS),
        )
        assert not verdict.allowed
        assert "sess-xyz" in verdict.reason
        assert "R+" in verdict.reason
        assert "confirm" in verdict.reason.lower()


class TestSafeKillExecutor:
    def test_safe_kill_does_not_signal_a_running_process(self) -> None:
        sent: list[tuple[int, int]] = []

        result = safe_kill(
            4242,
            hang_cause="high uptime",
            resolve_identity=_resolver(_FAILED_TARGET),
            sample_liveness=_sampler(_RUNNING_LIVENESS),
            send_signal=lambda pid, sig: sent.append((pid, sig)),
        )

        assert not result.allowed
        assert sent == []

    def test_safe_kill_does_not_signal_an_unknown_pid(self) -> None:
        sent: list[tuple[int, int]] = []

        result = safe_kill(
            4242,
            hang_cause="looks idle",
            resolve_identity=_resolver(_UNKNOWN_TARGET),
            sample_liveness=_sampler(_DEAD_LIVENESS),
            send_signal=lambda pid, sig: sent.append((pid, sig)),
        )

        assert not result.allowed
        assert sent == []

    def test_safe_kill_signals_a_verified_dead_target(self) -> None:
        sent: list[tuple[int, int]] = []

        result = safe_kill(
            4242,
            hang_cause="agent crashed, no output since",
            resolve_identity=_resolver(_FAILED_TARGET),
            sample_liveness=_sampler(_DEAD_LIVENESS),
            send_signal=lambda pid, sig: sent.append((pid, sig)),
        )

        assert result.allowed
        assert sent == [(4242, _expected_signal())]

    def test_safe_kill_raises_when_caller_requires_strict(self) -> None:
        sent: list[tuple[int, int]] = []
        with pytest.raises(SafeKillError, match="R\\+"):
            safe_kill(
                4242,
                hang_cause="looks idle",
                resolve_identity=_resolver(_FAILED_TARGET),
                sample_liveness=_sampler(_RUNNING_LIVENESS),
                send_signal=lambda pid, sig: sent.append((pid, sig)),
                strict=True,
            )
        assert sent == []


def _expected_signal() -> int:
    import signal  # noqa: PLC0415

    return signal.SIGTERM
