"""``t3 info artifacts <ticket>`` — read-only per-ticket artifact discovery (#273).

The "find our eggs" report: one place to locate, for a ticket, where its stack
lives (worktree on-disk path, per-worktree db_name, FSM state, live host ports),
its recorded plans (PlanArtifact), its run artifacts (each Task's
``result_artifact_path``), and its E2E evidence (E2eMandatoryRun spec + the
posted video/comment URL). Pure aggregation over EXISTING related rows — no new
storage model, no write, no FSM transition.

The live host ports are resolved through :func:`teatree.utils.ports.get_worktree_ports`
over the worktree's ``compose_project``; a worktree with no running stack simply
reports an empty port map. The model method
(:meth:`teatree.core.models.ticket.Ticket.artifacts`) stays pure — this command
is the single seam that injects the docker-touching resolver.
"""

import dataclasses
import json
from typing import Annotated

import typer
from django_typer.management import TyperCommand, command, initialize

from teatree.core.models import Ports, Ticket, TicketArtifacts, Worktree
from teatree.core.worktree.worktree_env import compose_project
from teatree.utils.ports import get_worktree_ports

_VALID_FORMATS = ("text", "json")


def _resolve_live_ports(worktree: Worktree) -> Ports:
    return get_worktree_ports(compose_project(worktree))


class Command(TyperCommand):
    @initialize()
    def init(self) -> None:
        """``t3 info`` group root."""

    @command()
    def artifacts(
        self,
        ticket_id: int,
        *,
        output_format: Annotated[
            str,
            typer.Option("--format", help="text (default) | json"),
        ] = "text",
    ) -> str:
        """Locate every artifact for a ticket: stack, ports, plans, run artifacts, E2E evidence.

        Returns the rendered report (django-typer writes it to stdout). The
        terse human view by default; the full structured aggregation as JSON
        with ``--format json``.
        """
        if output_format not in _VALID_FORMATS:
            allowed = " or ".join(repr(fmt) for fmt in _VALID_FORMATS)
            self.stderr.write(f"unknown --format {output_format!r}; use {allowed}")
            raise SystemExit(2)

        try:
            ticket = Ticket.objects.get(pk=ticket_id)
        except Ticket.DoesNotExist:
            self.stderr.write(f"  Ticket {ticket_id} not found")
            raise SystemExit(1) from None

        artifacts = ticket.artifacts(port_resolver=_resolve_live_ports)

        if output_format == "json":
            return json.dumps(dataclasses.asdict(artifacts), indent=2, sort_keys=False)
        return "\n".join(_render_text(ticket, artifacts))


def _render_text(ticket: Ticket, artifacts: TicketArtifacts) -> list[str]:
    """Render the terse human "find our eggs" view of a ticket's artifacts."""
    header = ticket.issue_url or f"ticket-{ticket.pk}"
    lines = [f"Artifacts for ticket #{artifacts.ticket_id} — {header}"]
    lines.extend(("", "Worktrees / stacks:"))
    lines.extend(_worktree_lines(artifacts))
    lines.extend(("", "Intake landscape survey:"))
    lines.extend(_landscape_lines(artifacts))
    lines.extend(("", "Plan artifacts:"))
    lines.extend(_plan_lines(artifacts))
    lines.extend(("", "Run artifacts (task results):"))
    lines.extend(_result_lines(artifacts))
    lines.extend(("", "E2E evidence:"))
    lines.extend(_e2e_lines(artifacts))
    return lines


def _worktree_lines(artifacts: TicketArtifacts) -> list[str]:
    if not artifacts.worktrees:
        return ["  (none)"]
    lines: list[str] = []
    for wt in artifacts.worktrees:
        ports = ", ".join(f"{name}={port}" for name, port in sorted(wt.ports.items())) or "(none running)"
        lines.extend(
            (
                f"  - {wt.repo_path} [{wt.state}] branch={wt.branch}",
                f"      path: {wt.worktree_path or '(not provisioned)'}",
                f"      db:   {wt.db_name or '(none)'}",
                f"      ports: {ports}",
            )
        )
    return lines


def _landscape_lines(artifacts: TicketArtifacts) -> list[str]:
    landscape = artifacts.landscape
    if landscape is None:
        return ["  (none)"]
    survey = landscape.survey
    counts = (
        f"open_prs={len(survey.get('open_prs', []))}, "
        f"in-flight worktrees={sum(1 for w in survey.get('worktrees', []) if w.get('in_flight'))}, "
        f"recommendations={len(survey.get('recommendations', []))}, "
        f"warnings={len(survey.get('warnings', []))}"
    )
    return [f"  - {landscape.recorded_at} by {landscape.recorded_by or '(unattributed)'}: {counts}"]


def _plan_lines(artifacts: TicketArtifacts) -> list[str]:
    if not artifacts.plan_artifacts:
        return ["  (none)"]
    return [
        f"  - {plan.recorded_at} by {plan.recorded_by or '(unattributed)'}: "
        f"{plan.plan_text.splitlines()[0] if plan.plan_text else ''}"
        for plan in artifacts.plan_artifacts
    ]


def _result_lines(artifacts: TicketArtifacts) -> list[str]:
    if not artifacts.result_artifact_paths:
        return ["  (none)"]
    return [f"  - {path}" for path in artifacts.result_artifact_paths]


def _e2e_lines(artifacts: TicketArtifacts) -> list[str]:
    if not artifacts.e2e_runs:
        return ["  (none)"]
    lines: list[str] = []
    for run in artifacts.e2e_runs:
        posted = run.posted_url or "(unposted — no video/comment URL)"
        lines.extend(
            (
                f"  - {run.spec} [{run.result}] @ {run.head_sha[:8]}",
                f"      evidence: {posted}",
            )
        )
    return lines
