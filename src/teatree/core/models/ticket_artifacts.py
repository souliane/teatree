"""Read-only per-ticket artifact discovery aggregation (#273).

The artifact-discovery report over a ticket's EXISTING related rows — no new
storage model, no FSM transition, no write. ``collect_ticket_artifacts`` joins,
for a ticket: its worktrees/stacks (on-disk path, db_name, FSM state,
repo/branch, live host ports), its ``PlanArtifact`` rows, each
``Task.result_artifact_path``, and its ``E2eMandatoryRun`` evidence (spec + the
posted video/comment URL).

Lives in its own module (not on ``ticket.py``) so the model file stays under the
module-health LOC cap; ``Ticket.artifacts`` is a thin delegate. The collector
takes the ``Ticket`` instance rather than living on the model to keep this a
focused, importable aggregation seam.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from teatree.core.models.types import Ports

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket
    from teatree.core.models.worktree import Worktree


# Resolves the live host ports for a worktree (e.g. ``get_worktree_ports`` over
# docker compose). Injected so the aggregation itself stays pure — no docker I/O
# — and is deterministic under test.
PortResolver = Callable[["Worktree"], Ports]


@dataclass(frozen=True, slots=True)
class WorktreeArtifact:
    """A worktree's locating signals for the per-ticket artifact aggregation (#273).

    The on-disk worktree path, the per-worktree database name, the FSM state,
    the repo identifier + branch, and the live host ports (empty when no stack
    is running, or when no port resolver was supplied).
    """

    repo_path: str
    branch: str
    state: str
    db_name: str
    worktree_path: str
    ports: Ports = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PlanArtifactRef:
    """A PlanArtifact row's content for the per-ticket artifact aggregation (#273)."""

    plan_text: str
    recorded_by: str
    recorded_at: str


@dataclass(frozen=True, slots=True)
class E2eRunRef:
    """An E2eMandatoryRun's evidence for the per-ticket artifact aggregation (#273).

    ``posted_url`` is the SHA-bound ``e2e post-evidence`` comment — the place the
    E2E video/evidence lives — and is empty for a recorded-but-unposted run.
    """

    spec: str
    result: str
    head_sha: str
    posted_url: str
    recorded_at: str


@dataclass(frozen=True, slots=True)
class TicketArtifacts:
    """Read-only artifact-discovery aggregation over a ticket's existing rows (#273).

    Collected by :func:`collect_ticket_artifacts` from the related Worktree,
    PlanArtifact, Task and E2eMandatoryRun rows — no new storage model. Every
    collection is a tuple so the aggregation is immutable.
    """

    ticket_id: int
    worktrees: tuple[WorktreeArtifact, ...] = ()
    plan_artifacts: tuple[PlanArtifactRef, ...] = ()
    result_artifact_paths: tuple[str, ...] = ()
    e2e_runs: tuple[E2eRunRef, ...] = ()


def collect_ticket_artifacts(ticket: "Ticket", *, port_resolver: PortResolver | None = None) -> TicketArtifacts:
    """Aggregate every locatable artifact for *ticket* — read-only (#273).

    ``port_resolver`` resolves the live host ports for a worktree (the CLI
    passes :func:`teatree.utils.ports.get_worktree_ports` via
    ``compose_project``). When omitted no docker I/O happens and every
    worktree's ``ports`` is the empty dict — keeping the collector pure and
    deterministic for callers that only need the durable DB signals.
    """
    from django.apps import apps  # noqa: PLC0415

    worktree_model = apps.get_model("core", "Worktree")
    worktrees = tuple(
        WorktreeArtifact(
            repo_path=wt.repo_path,
            branch=wt.branch,
            state=wt.state,
            db_name=wt.db_name,
            worktree_path=wt.worktree_path,
            ports=port_resolver(wt) if port_resolver is not None else {},
        )
        for wt in worktree_model.objects.filter(ticket=ticket).order_by("pk")
    )
    plans = tuple(
        PlanArtifactRef(
            plan_text=plan.plan_text,
            recorded_by=plan.recorded_by,
            recorded_at=plan.recorded_at.isoformat(),
        )
        for plan in ticket.plan_artifacts.order_by("-recorded_at")  # ty: ignore[unresolved-attribute]
    )
    result_paths = tuple(
        ticket.tasks.exclude(result_artifact_path="")  # ty: ignore[unresolved-attribute]
        .order_by("pk")
        .values_list("result_artifact_path", flat=True)
    )
    e2e_runs = tuple(
        E2eRunRef(
            spec=run.spec,
            result=run.result,
            head_sha=run.head_sha,
            posted_url=run.posted_url,
            recorded_at=run.recorded_at.isoformat(),
        )
        for run in ticket.e2e_mandatory_runs.order_by("-recorded_at")  # ty: ignore[unresolved-attribute]
    )
    return TicketArtifacts(
        ticket_id=int(ticket.pk),
        worktrees=worktrees,
        plan_artifacts=plans,
        result_artifact_paths=result_paths,
        e2e_runs=e2e_runs,
    )
