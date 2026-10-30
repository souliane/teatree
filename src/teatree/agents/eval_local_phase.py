"""Run the scheduled behavioral eval on the factory's subscription account."""

import os
import signal
import time
from contextlib import suppress

from teatree.core.models import EvalRunRecord, EvalVerdict, Loop, Task
from teatree.utils.run import DEVNULL, PIPE, TimeoutExpired, redact_secrets, run_allowed_to_fail, spawn_session_leader

_HEARTBEAT_SECONDS = 60
_CONTROL_TIMEOUT_SECONDS = 30
_SECONDS_PER_SCENARIO = 15 * 60


def _command(*args: str) -> list[str]:
    return ["t3", "eval", *args]


def _subscription_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def _shard() -> str:
    delay = Loop.objects.filter(name="eval_local").values_list("delay_seconds", flat=True).first()
    days = 1 if delay is None else max(1, round(delay / 86400))
    return f"rotate/{max(1, round(7 / days))}"


def _selected_scenarios(shard: str, env: dict[str, str]) -> list[str]:
    result = run_allowed_to_fail(
        _command("list", "--names-only", "--shard", shard),
        env=env,
        expected_codes=None,
        timeout=_CONTROL_TIMEOUT_SECONDS,
    )
    if result.returncode:
        msg = f"measured nothing: scenario catalog unavailable: {result.stderr.strip()}"
        raise RuntimeError(msg)
    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not names:
        msg = "measured nothing: selected scenario shard is empty"
        raise RuntimeError(msg)
    return names


def _run_child(task: Task, shard: str, selected: list[str], env: dict[str, str]) -> tuple[int, str, str]:
    child = spawn_session_leader(
        _command(
            "run", "--backend", "api", "--credential", "subscription_oauth", "--local", "--persist", "--shard", shard
        ),
        env=env,
        stdio=(DEVNULL, PIPE, PIPE),
    )
    deadline = time.monotonic() + len(selected) * _SECONDS_PER_SCENARIO
    stopped = False
    try:
        while True:
            try:
                timeout = min(_HEARTBEAT_SECONDS, max(0.01, deadline - time.monotonic()))
                stdout, stderr = child.communicate(timeout=timeout)
            except TimeoutExpired:
                if time.monotonic() >= deadline:
                    os.killpg(child.pid, signal.SIGKILL)
                    stopped = True
                    stdout, stderr = child.communicate()
                    return 124, stdout or "", stderr or ""
                task.renew_lease(lease_seconds=3 * _HEARTBEAT_SECONDS)
            else:
                if child.returncode is None:
                    msg = "eval process ended without an exit status"
                    raise RuntimeError(msg)
                return child.returncode, stdout or "", stderr or ""
    finally:
        if not stopped and child.returncode is None:
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            child.communicate()


def _record_unmeasured(selected: list[str], *, reason: str, suite: str) -> EvalRunRecord:
    run = EvalRunRecord.objects.create(model="subscription_oauth", suite=suite)
    for name in selected:
        run.record_scenario(scenario_name=name, verdict=EvalVerdict.SKIP, terminal_reason=reason)
    return run


def run_eval_local(task: Task) -> str:
    env = _subscription_env()
    suite = f"{EvalRunRecord.LOCAL_SUITE_PREFIX}{task.pk}"
    env[EvalRunRecord.RECORD_SUITE_ENV] = suite
    shard = _shard()
    try:
        selected = _selected_scenarios(shard, env)
    except (OSError, RuntimeError, TimeoutExpired) as exc:
        _record_unmeasured([], reason=str(exc), suite=suite)
        raise
    try:
        pinned = run_allowed_to_fail(
            _command("pinned-regressions", "--strict"),
            env=env,
            expected_codes=None,
            timeout=_CONTROL_TIMEOUT_SECONDS,
        )
    except (OSError, RuntimeError, TimeoutExpired) as exc:
        msg = f"measured nothing: pinned-regressions unavailable: {redact_secrets(str(exc))}"
        _record_unmeasured(selected, reason=msg, suite=suite)
        raise RuntimeError(msg) from exc
    prior_pk = EvalRunRecord.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
    try:
        child_exit, stdout, stderr = _run_child(task, shard, selected, env)
    except OSError as exc:
        msg = f"measured nothing: eval lane unavailable: {redact_secrets(str(exc))}"
        _record_unmeasured(selected, reason=msg, suite=suite)
        raise RuntimeError(msg) from exc
    detail = redact_secrets((stderr or stdout).strip())[-1000:]
    run = EvalRunRecord.objects.filter(pk__gt=prior_pk, suite=suite).order_by("-pk").first()
    if run is None:
        reason = f"eval lane exited {child_exit} without a recorded run: {detail}".rstrip(": ")
        run = _record_unmeasured(selected, reason=reason, suite=suite)
    if run.measured_nothing:
        msg = f"measured nothing: 0 of {run.total} scenarios graded (child exit {child_exit}): {detail}".rstrip(": ")
        raise RuntimeError(msg)
    if pinned.returncode:
        diagnostic = redact_secrets((pinned.stderr or pinned.stdout).strip())[-1000:]
        msg = (f"pinned-regressions failed (exit {pinned.returncode}) after eval run #{run.pk}: {diagnostic}").rstrip(
            ": "
        )
        raise RuntimeError(msg)
    return f"eval run #{run.pk}: {run.passed} passed, {run.failed} failed, {run.errored} errors; {shard}"
