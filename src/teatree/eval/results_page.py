"""Render the behavioral eval results from the durable run ledger."""

from datetime import datetime, timedelta

from django.utils import timezone

from teatree.core.models import EvalRunRecord, EvalVerdict


def render_results_page(*, now: datetime | None = None) -> str:
    current = now or timezone.now()
    run = EvalRunRecord.objects.order_by("-started_at", "-pk").first()
    if run is None:
        return "# Behavioral eval results\n\nno recorded runs\n"
    lines = [f"# Behavioral eval results — {run.started_at.date().isoformat()}", ""]
    if current - run.started_at > timedelta(days=7):
        lines.extend((f"STALE: latest run is older than one week ({run.started_at.isoformat()}).", ""))
    if run.measured_nothing:
        lines.append(f"MEASURED NOTHING: 0 of {run.total} scenarios graded ({run.skipped} skipped).")
    else:
        cost = sum(run.results.values_list("cost_usd", flat=True))
        lines.append(
            f"{run.passed} passed, {run.failed} failed, {run.errored} errors, "
            f"{run.skipped} skipped (of {run.total}); cost ${cost:.4f}"
        )
        failures = run.results.filter(verdict=EvalVerdict.FAIL).order_by("scenario_name")
        if failures.exists():
            lines.extend(("", "## Failing scenarios", ""))
            lines.extend(f"- {row.scenario_name}" for row in failures)
    return "\n".join(lines) + "\n"
