"""Bounded transient-throttle retry envelope for the metered in-process eval runner.

The driver rides out a TRANSIENT/SUSTAINED throttle (and the empty-trajectory
watchdog timeout) with bounded backoff, while a genuine cap, credit exhaustion, or
mislabeled success is graded by the caller's handlers — never retried, the
anti-cheat boundary that keeps a real behavioral fail from becoming a passing retry.
"""

from collections.abc import Callable

import pytest

from teatree.eval.api_errors import SuccessMislabelResultError, TerminalResultError
from teatree.eval.ephemeral_checkout import EphemeralCheckoutError
from teatree.eval.models import EvalRun
from teatree.eval.throttle_retry import (
    THROTTLE_RETRY_MAX_ATTEMPTS,
    THROTTLE_WINDOW_WAIT_MAX_SECONDS,
    ThrottleRetryDriver,
    ThrottleRetryHandlers,
    resolve_throttle_retries,
)
from teatree.llm.anthropic_limits import CreditExhaustedError

#: The verbatim string the SDK message reader surfaces when the subprocess CLI
#: dies mid-stream with no ``result`` event (the "Fatal error in message reader"
#: transport crash that aborted a whole eval run) — no trajectory, safe to re-run.
_TRANSPORT_CRASH_MESSAGE = (
    "Command failed with exit code 1 (exit code: 1)\nError output: Check stderr output for details"
)
#: The verbatim message an `EphemeralCheckoutError` carries when a host RAM spike
#: makes the per-run ephemeral-checkout `git clone` fail — the second transient shape
#: that aborted a whole eval run before this fix. No trajectory, safe to re-run.
_EPHEMERAL_GIT_CLONE_MESSAGE = (
    "cannot provision an isolated ephemeral checkout at /tmp/t3-eval-ephemeral-checkout-x/teatree: "
    "git clone failed. The sub-agent-spawning scenario REFUSES to run on the real clone."
)


def _run(*, throttle_retries: int = 0, is_error: bool = False, terminal_reason: str = "success") -> EvalRun:
    return EvalRun(
        spec_name="s",
        tool_calls=(),
        text_blocks=(),
        terminal_reason=terminal_reason,
        is_error=is_error,
        raw_stdout="",
        raw_stderr="",
        throttle_retries=throttle_retries,
    )


def _handlers() -> ThrottleRetryHandlers:
    return ThrottleRetryHandlers(
        grade_success=lambda messages, retries: _run(throttle_retries=retries),
        grade_cap=lambda cap: _run(terminal_reason="capped", is_error=True),
        grade_mislabel=lambda mislabel: _run(terminal_reason="mislabel"),
        surface_throttled=lambda reason, retries: _run(terminal_reason=reason, is_error=True, throttle_retries=retries),
    )


def _scripted_drive(script: list[BaseException | list]) -> tuple[Callable[[], list], dict[str, int]]:
    calls: dict[str, int] = {"n": 0}

    def _drive() -> list:
        index = min(calls["n"], len(script) - 1)
        calls["n"] += 1
        step = script[index]
        if isinstance(step, BaseException):
            raise step
        return step

    return _drive, calls


def _driver(sleeps: list[float], *, max_attempts: int = THROTTLE_RETRY_MAX_ATTEMPTS) -> ThrottleRetryDriver:
    return ThrottleRetryDriver(max_attempts=max_attempts, sleep=sleeps.append, rand=lambda: 0.0)


class TestThrottleRetryDriver:
    def test_transient_throttle_retried_then_success(self) -> None:
        drive, calls = _scripted_drive([RuntimeError("Overloaded"), []])
        sleeps: list[float] = []
        run = _driver(sleeps).run(drive, _handlers())
        assert calls["n"] == 2
        assert run.throttle_retries == 1
        assert sleeps == [pytest.approx(1.0)]

    def test_repeated_throttles_back_off_exponentially(self) -> None:
        drive, calls = _scripted_drive([RuntimeError("Overloaded")] * 3 + [[]])
        sleeps: list[float] = []
        run = _driver(sleeps).run(drive, _handlers())
        assert calls["n"] == 4
        assert run.throttle_retries == 3
        assert sleeps == [pytest.approx(1.0), pytest.approx(2.0), pytest.approx(4.0)]

    def test_transport_crash_is_retried_then_success(self) -> None:
        # A mid-stream SDK transport crash aborted the whole 2.5h run before this
        # fix; it carries NO trajectory, so it is ridden out like any transient
        # throttle and the retried attempt succeeds.
        drive, calls = _scripted_drive([RuntimeError(_TRANSPORT_CRASH_MESSAGE), []])
        sleeps: list[float] = []
        run = _driver(sleeps).run(drive, _handlers())
        assert calls["n"] == 2
        assert run.throttle_retries == 1
        assert sleeps == [pytest.approx(1.0)]

    def test_ephemeral_checkout_transient_is_retried_then_success(self) -> None:
        # A per-run ephemeral-checkout provision failure (a RAM-spike git-clone abort)
        # carries NO trajectory; like any transient it is ridden out and the retried
        # attempt succeeds — one flaky clone no longer kills the whole suite.
        drive, calls = _scripted_drive([EphemeralCheckoutError(_EPHEMERAL_GIT_CLONE_MESSAGE), []])
        sleeps: list[float] = []
        run = _driver(sleeps).run(drive, _handlers())
        assert calls["n"] == 2
        assert run.throttle_retries == 1
        assert sleeps == [pytest.approx(1.0)]

    def test_genuine_cap_is_graded_not_retried(self) -> None:
        cap = TerminalResultError(terminal_reason="max_turns", messages=[], cause=RuntimeError("max turns"))
        drive, calls = _scripted_drive([cap, []])
        sleeps: list[float] = []
        run = _driver(sleeps).run(drive, _handlers())
        assert calls["n"] == 1  # NOT retried — the cap surfaces immediately
        assert run.terminal_reason == "capped"
        assert sleeps == []

    def test_success_mislabel_is_graded_not_retried(self) -> None:
        mislabel = SuccessMislabelResultError(messages=[], cause=RuntimeError("mislabel"))
        drive, calls = _scripted_drive([mislabel, []])
        sleeps: list[float] = []
        run = _driver(sleeps).run(drive, _handlers())
        assert calls["n"] == 1
        assert run.terminal_reason == "mislabel"

    def test_credit_exhaustion_propagates_not_retried(self) -> None:
        drive, calls = _scripted_drive([CreditExhaustedError("credits gone"), []])
        sleeps: list[float] = []
        with pytest.raises(CreditExhaustedError):
            _driver(sleeps).run(drive, _handlers())
        assert calls["n"] == 1
        assert sleeps == []

    def test_genuine_crash_re_raises_not_retried(self) -> None:
        drive, calls = _scripted_drive([RuntimeError("TypeError: not subscriptable"), []])
        sleeps: list[float] = []
        with pytest.raises(RuntimeError, match="not subscriptable"):
            _driver(sleeps).run(drive, _handlers())
        assert calls["n"] == 1
        assert sleeps == []

    def test_empty_trajectory_timeout_is_retried(self) -> None:
        drive, calls = _scripted_drive([TimeoutError(), []])
        sleeps: list[float] = []
        run = _driver(sleeps).run(drive, _handlers())
        assert calls["n"] == 2
        assert run.throttle_retries == 1

    def test_session_window_wait_is_bounded_by_the_cap(self) -> None:
        drive, calls = _scripted_drive([RuntimeError("session limit reached"), []])
        sleeps: list[float] = []
        run = _driver(sleeps).run(drive, _handlers())
        assert calls["n"] == 2
        assert run.is_error is False
        assert sleeps == [pytest.approx(THROTTLE_WINDOW_WAIT_MAX_SECONDS)]

    def test_exhausted_throttle_surfaces_loud(self) -> None:
        drive, calls = _scripted_drive([RuntimeError("Overloaded")])
        sleeps: list[float] = []
        driver = ThrottleRetryDriver(max_attempts=2, sleep=sleeps.append, rand=lambda: 0.0)
        run = driver.run(drive, _handlers())
        assert calls["n"] == 3  # initial attempt + 2 bounded retries, all throttled
        assert run.is_error is True
        assert run.throttle_retries == 2
        assert "throttled" in run.terminal_reason
        assert sleeps == [pytest.approx(1.0), pytest.approx(2.0)]

    def test_zero_max_attempts_disables_retry(self) -> None:
        drive, calls = _scripted_drive([RuntimeError("Overloaded")])
        sleeps: list[float] = []
        driver = ThrottleRetryDriver(max_attempts=0, sleep=sleeps.append, rand=lambda: 0.0)
        run = driver.run(drive, _handlers())
        assert calls["n"] == 1
        assert run.is_error is True
        assert sleeps == []


class TestResolveThrottleRetries:
    def test_default_when_unset(self, monkeypatch) -> None:
        monkeypatch.delenv("T3_EVAL_THROTTLE_RETRIES", raising=False)
        assert resolve_throttle_retries() == THROTTLE_RETRY_MAX_ATTEMPTS

    def test_zero_is_honored(self, monkeypatch) -> None:
        monkeypatch.setenv("T3_EVAL_THROTTLE_RETRIES", "0")
        assert resolve_throttle_retries() == 0

    def test_negative_falls_back(self, monkeypatch) -> None:
        monkeypatch.setenv("T3_EVAL_THROTTLE_RETRIES", "-1")
        assert resolve_throttle_retries() == THROTTLE_RETRY_MAX_ATTEMPTS

    def test_unparsable_falls_back(self, monkeypatch) -> None:
        monkeypatch.setenv("T3_EVAL_THROTTLE_RETRIES", "abc")
        assert resolve_throttle_retries() == THROTTLE_RETRY_MAX_ATTEMPTS
