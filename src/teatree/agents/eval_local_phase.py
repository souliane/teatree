"""Run the scheduled behavioral eval on the factory's subscription account."""

import os
import signal
import sys
import time

from teatree.core.models import EvalRunRecord, EvalVerdict, Loop, Task
from teatree.utils.run import DEVNULL, PIPE, TimeoutExpired, run_allowed_to_fail, spawn_session_leader

_HEARTBEAT_SECONDS = 60
_SECONDS_PER_SCENARIO = 15 * 60


def _command(*args: str) -> list[str]:
    return [sys.executable, "-m", "teatree", "eval", *args]


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
    )
    if result.returncode:
        msg = f"measured nothing: scenario catalog unavailable: {result.stderr.strip()}"
        raise RuntimeError(msg)
    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not names:
        msg = "measured nothing: selected scenario shard is empty"
        raise RuntimeError(msg)
    return names


def _run_child(task: Task, shard: str, selected: list[str], env: dict[str, str]) -> int:
    child = spawn_session_leader(
        _command(
            "run", "--backend", "api", "--credential", "subscription_oauth", "--local", "--persist", "--shard", shard
        ),
        env=env,
        stdio=(DEVNULL, PIPE, PIPE),
    )
    deadline = time.monotonic() + len(selected) * _SECONDS_PER_SCENARIO
    while True:
        try:
            child.communicate(timeout=min(_HEARTBEAT_SECONDS, max(0.01, deadline - time.monotonic())))
        except TimeoutExpired:
            if time.monotonic() >= deadline:
                os.killpg(child.pid, signal.SIGKILL)
                child.communicate()
                return 124
            task.renew_lease(lease_seconds=3 * _HEARTBEAT_SECONDS)
        else:
            return child.returncode


def _record_unmeasured(selected: list[str], *, reason: str) -> EvalRunRecord:
    run = EvalRunRecord.objects.create(model="subscription_oauth", suite="eval_local")
    for name in selected:
        run.record_scenario(scenario_name=name, verdict=EvalVerdict.SKIP, terminal_reason=reason)
    return run


def run_eval_local(task: Task) -> str:
    env = _subscription_env()
    shard = _shard()
    selected = _selected_scenarios(shard, env)
    pinned = run_allowed_to_fail(
        _command("pinned-regressions"),
        env=env,
        expected_codes=None,
    )
    prior_pk = EvalRunRecord.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
    child_exit = _run_child(task, shard, selected, env)
    run = EvalRunRecord.objects.filter(pk__gt=prior_pk).order_by("-pk").first()
    if run is None:
        run = _record_unmeasured(selected, reason=f"eval lane exited {child_exit} without a recorded run")
    if run.measured_nothing:
        msg = f"measured nothing: 0 of {run.total} scenarios graded (child exit {child_exit})"
        raise RuntimeError(msg)
    if pinned.returncode:
        msg = f"pinned-regressions failed (exit {pinned.returncode}) after eval run #{run.pk}"
        raise RuntimeError(msg)
    return f"eval run #{run.pk}: {run.passed} passed, {run.failed} failed, {run.errored} errors; {shard}"
