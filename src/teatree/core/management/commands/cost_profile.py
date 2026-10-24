"""``t3 cost-profile`` — where the fleet's money goes, measured before anything is changed.

Reads REAL dispatches only (non-park, output tokens recorded, usage known) and reports the
cache read share, the resume-context cost and the per-item addressable upper bounds with their
union. An empty sample, a sample with no ``context_tokens``, or an unreadable store exits non-zero
naming the cause: an unmeasured item is never printed as 0%.

Read-only: every query underneath is a select. The report is routed through the machine-output
seam — JSON on stdout under ``--json``, the human view on stderr.
"""

import datetime as dt
from typing import IO, Annotated, NoReturn, cast

import typer
from django.db import DatabaseError
from django.utils import timezone

from teatree.core.cost_baseline.profile import CostProfile, CostProfileError, ProfiledAttempt, render_lines
from teatree.core.machine_output import MachineOutputCommand, emit
from teatree.core.models.task_attempt import TaskAttempt


def _parse_since(raw: str) -> dt.datetime | None:
    value = raw.strip()
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        msg = f"--since is not a valid ISO-8601 datetime: {value!r}"
        raise CostProfileError(msg) from exc
    return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed


class Command(MachineOutputCommand):
    # TyperCommand doesn't override BaseCommand.handle; the typed return is MachineOutputCommand's contract.
    def handle(  # ty: ignore[invalid-method-override]
        self,
        *,
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the structured report as JSON instead of the human view."),
        ] = False,
        since: Annotated[
            str,
            typer.Option("--since", help="ISO-8601 lower bound on attempt start (default: every attempt)."),
        ] = "",
    ) -> CostProfile:
        """Profile real dispatches: read share, resume-context cost and addressable upper bounds."""
        try:
            attempts = TaskAttempt.objects.real_dispatches().order_by("pk")
            if (lower_bound := _parse_since(since)) is not None:
                attempts = attempts.filter(started_at__gte=lower_bound)
            profile = CostProfile.build(
                ProfiledAttempt.of_all(attempts, trail=TaskAttempt.objects.context_trail()),
            )
        except CostProfileError as exc:
            self._refuse(str(exc))
        except DatabaseError as exc:
            self._refuse(f"control DB read failed: {exc}")

        self.print_result = False
        emit(
            profile,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human="\n".join(render_lines(profile)) + "\n",
        )
        return profile

    def _refuse(self, cause: str) -> NoReturn:
        self.stderr.write(f"cost-profile: {cause}\n")
        raise SystemExit(1)
