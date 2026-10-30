"""Live health state of the recorded behavioral evals."""

from datetime import datetime, timedelta
from importlib import import_module

from teatree.core.factory.health_signal import HealthSignal
from teatree.core.models import EvalRunRecord, KnownIssue, Loop

_STALE_AFTER = timedelta(days=7)


def eval_measurement_signal(now: datetime) -> HealthSignal | None:
    if not import_module("teatree.loops.enable_verdict").loop_admits("eval_local", now):
        return None
    local_runs = EvalRunRecord.objects.filter(suite__startswith=EvalRunRecord.LOCAL_SUITE_PREFIX)
    latest = local_runs.order_by("-started_at", "-pk").first()
    if latest is None:
        return None
    last_measured = (
        local_runs.filter(scenario_results__verdict__in=("pass", "fail"))
        .distinct()
        .order_by("-started_at", "-pk")
        .first()
    )
    delay = Loop.objects.filter(name="eval_local").values_list("delay_seconds", flat=True).first() or 0
    critical_after = max(_STALE_AFTER, timedelta(seconds=2 * delay))
    first = local_runs.earliest("started_at")
    last_measured_at = last_measured.started_at if last_measured else first.started_at
    age = now - last_measured_at
    if age >= critical_after:
        return HealthSignal(
            fingerprint="eval-local-measured-nothing",
            kind="eval_measurement",
            severity=KnownIssue.Severity.CRITICAL,
            summary=f"behavioral evals measured nothing within {critical_after.days} days",
        )
    if latest.measured_nothing or latest.total == 0:
        return HealthSignal(
            fingerprint="eval-local-measured-nothing",
            kind="eval_measurement",
            severity=KnownIssue.Severity.WARNING,
            summary=f"behavioral evals measured nothing: 0 of {latest.total} scenarios graded",
        )
    if age > _STALE_AFTER:
        return HealthSignal(
            fingerprint="eval-local-measured-nothing",
            kind="eval_measurement",
            severity=KnownIssue.Severity.WARNING,
            summary=f"behavioral evals stale: last measured {last_measured_at.date().isoformat()}",
        )
    return None
