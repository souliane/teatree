"""Plan-gate operator command logic, factored out of ``ticket.py``.

The ``t3 <overlay> ticket`` group exposes four plan-gate operator commands —
``plan`` (record a real plan), ``plan-bypass`` (audited ``--human-authorize``
bypass), ``skip-planning`` (lightweight trivial-work carve-out), and
``plan-reconcile-inflight`` (retroactive one-time advance). They share one shape:
validate input, then in a single ``transaction.atomic`` block produce a
satisfying signal (a ``PlanArtifact`` or a ``trivial_plan_skip`` marker), drive
``ticket.plan()`` STARTED → PLANNED, and ``save()``.

This module owns that shared shape so the command methods in ``ticket.py`` stay
thin delegators (one cohesive concern, one home — and ``ticket.py`` stays under
its module-health LOC cap). The command methods keep the django-typer
``@command`` decoration + CLI signature; the work lives here.
"""

from typing import TYPE_CHECKING, TypedDict

from django.db import transaction
from django_fsm import TransitionNotAllowed

from teatree.core.models import Ticket
from teatree.core.models.errors import InvalidTransitionError
from teatree.core.models.plan_adequacy import declared_seam_paths, is_valid_base_sha
from teatree.core.models.plan_artifact import PlanArtifact
from teatree.core.models.ticket_worktree_checks import _resolve_base_branch, dispatch_worktree_path
from teatree.core.models.trivial_plan_skip import mark_trivial_plan_skip
from teatree.core.models.types import PlanAdequacy
from teatree.core.worktree.branch_currency import commits_between_touching_paths, fetch_target_head

if TYPE_CHECKING:
    from collections.abc import Callable


class PlanResult(TypedDict, total=False):
    ticket_id: int
    artifact_id: int
    state: str
    error: str


class PlanReconcileResult(TypedDict, total=False):
    inspected: int
    bypassed: int
    skipped: int


class PlanAdvanceError(Exception):
    """The plan() advance was refused; ``message`` is the surfaced reason."""

    def __init__(self, ticket: Ticket, message: str) -> None:
        super().__init__(message)
        self.ticket = ticket
        self.message = message


def record_artifact_and_advance(
    *,
    ticket: Ticket,
    plan_text: str,
    recorded_by: str,
    base_sha: str = "",
    adequacy: PlanAdequacy | dict | None = None,
) -> "PlanArtifact":
    """Record a PlanArtifact and drive ``ticket.plan()`` in one atomic block.

    Raises :class:`PlanAdvanceError` (carrying the ticket + surfaced reason) when
    the artifact factory rejects the input or the FSM refuses the transition, so
    a failed advance rolls back the artifact write and the caller can return a
    structured error. ``base_sha``/``adequacy`` carry the late-bound-plan +
    adequacy manifest (SELFCATCH-3). The audited human-authorized escape is the
    sibling :func:`record_bypass_and_advance`.
    """
    return _advance_with(
        ticket,
        lambda: PlanArtifact.record(
            ticket=ticket, plan_text=plan_text, recorded_by=recorded_by, base_sha=base_sha, adequacy=adequacy
        ),
    )


def record_bypass_and_advance(*, ticket: Ticket, plan_text: str, recorded_by: str) -> "PlanArtifact":
    """Record an adequacy-exempt bypass PlanArtifact and drive ``ticket.plan()`` (SELFCATCH-3).

    The audited-bypass sibling of :func:`record_artifact_and_advance`; delegates to
    :meth:`PlanArtifact.record_bypass` so the write is exempt from the strict
    ``require_plan_adequacy`` enforcement.
    """
    return _advance_with(
        ticket, lambda: PlanArtifact.record_bypass(ticket=ticket, plan_text=plan_text, recorded_by=recorded_by)
    )


def _advance_with(ticket: Ticket, make_artifact: "Callable[[], PlanArtifact]") -> "PlanArtifact":
    """Create an artifact via *make_artifact* then drive ``ticket.plan()`` in one atomic block."""
    try:
        with transaction.atomic():
            artifact = make_artifact()
            ticket.plan()
            ticket.save()
    except (ValueError, TransitionNotAllowed, InvalidTransitionError) as exc:
        raise PlanAdvanceError(ticket, str(exc)) from exc
    return artifact


def record_trivial_skip_and_advance(*, ticket: Ticket, reason: str, by: str) -> None:
    """Record a trivial-skip marker and drive ``ticket.plan()`` in one atomic block.

    The lightweight sibling of :func:`record_artifact_and_advance` — no
    ``PlanArtifact`` is written; the marker is the satisfying signal. Raises
    :class:`PlanAdvanceError` on a rejected marker or a refused transition (the
    atomic block rolls the marker write back).
    """
    try:
        with transaction.atomic():
            mark_trivial_plan_skip(ticket, reason=reason, by=by)
            ticket.plan()
            ticket.save()
    except (ValueError, TransitionNotAllowed, InvalidTransitionError) as exc:
        raise PlanAdvanceError(ticket, str(exc)) from exc


def reconcile_inflight(*, authorizer: str, issue_ref: str, dry_run: bool) -> tuple[PlanReconcileResult, list[str]]:
    """Retroactively advance every STARTED ticket to PLANNED via an audited bypass.

    Returns the tally plus a list of human-readable log lines for the caller to
    emit. A per-ticket transition refusal is recorded as skipped, never raised,
    so one stuck ticket does not abort the sweep.
    """
    started = list(Ticket.objects.filter(state=Ticket.State.STARTED))
    log: list[str] = [f"  found {len(started)} STARTED ticket(s)"]
    bypassed = 0
    skipped = 0
    for ticket in started:
        reason = "retroactive — PLANNED state added mid-flight" + (f" ({issue_ref})" if issue_ref else "")
        if dry_run:
            log.append(f"  [dry-run] would bypass ticket {ticket.pk}: {reason}")
            skipped += 1
            continue
        try:
            with transaction.atomic():
                PlanArtifact.record_bypass(
                    ticket=ticket,
                    plan_text=f"[audited bypass by {authorizer}] {reason}",
                    recorded_by=authorizer,
                )
                ticket.plan()
                ticket.save()
            log.append(f"  ticket {ticket.pk}: STARTED → PLANNED (bypass recorded)")
            bypassed += 1
        except (ValueError, TransitionNotAllowed, InvalidTransitionError) as exc:
            log.append(f"  ticket {ticket.pk}: skipped — {exc}")
            skipped += 1
    return PlanReconcileResult(inspected=len(started), bypassed=bypassed, skipped=skipped), log


class ReaffirmError(Exception):
    """A plan-reaffirm was refused; ``message`` is the surfaced reason."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def reaffirm_plan(
    *,
    ticket: Ticket,
    new_base_sha: str,
    dispositions: list[str],
    by: str,
    fresh_adequacy: dict | None = None,
) -> "PlanArtifact":
    """Append a NEW PlanArtifact bound to ``new_base_sha`` (SELFCATCH-3 late-bound remediation).

    Handles BOTH remediation cases the plan-currency gate can name. STALE-but-adequate:
    the prior plan's manifest is adequate but its base moved — carry the manifest
    forward, re-bind to ``new_base_sha``, and REFUSE with :class:`ReaffirmError` unless
    a ``--disposition`` is supplied per intervening commit that touched a declared seam
    (``git log old..new -- <seams>``); a stale-base re-bind reckons with every seam
    change, never rubber-stamps it. INADEQUATE/legacy: the prior plan has no adequate
    manifest to carry (a legacy blank-adequacy row), so the operator supplies a fresh
    complete manifest via ``fresh_adequacy`` to turn it into an adequate, current plan.

    Either way the final ``record`` re-runs the adequacy enforcement; a residual failure
    (no fresh manifest for an inadequate plan) is surfaced as a clean
    :class:`ReaffirmError` naming the working remediations, never a raw ``ValueError``.
    """
    cleaned_new = new_base_sha.strip()
    if not is_valid_base_sha(cleaned_new):
        msg = f"--base-sha must be a full 40-char hex SHA (got {cleaned_new[:12]!r})"
        raise ReaffirmError(msg)

    latest = PlanArtifact.objects.filter(ticket=ticket).order_by("-recorded_at", "-pk").first()
    if latest is None:
        msg = f"ticket {ticket.pk} has no plan to reaffirm — record one with `ticket plan` first"
        raise ReaffirmError(msg)

    seams = declared_seam_paths(latest.adequacy)
    intervening: tuple[str, ...] = ()
    repo = dispatch_worktree_path(ticket)
    if repo and seams:
        fetch_target_head(repo, _resolve_base_branch(repo))
        found = commits_between_touching_paths(repo, latest.base_sha, cleaned_new, seams)
        intervening = found or ()

    if intervening and len(dispositions) < len(intervening):
        listed = "\n".join(f"    - {sha[:12]}" for sha in intervening)
        msg = (
            f"refusing to reaffirm ticket {ticket.pk}: {len(intervening)} intervening commit(s) touched a "
            f"declared seam ({', '.join(seams)}) and only {len(dispositions)} disposition(s) were given. "
            f"Pass one `--disposition <how-this-commit-affects-the-plan>` per intervening commit:\n{listed}"
        )
        raise ReaffirmError(msg)

    manifest = dict(fresh_adequacy) if fresh_adequacy else dict(latest.adequacy)
    trail = f"[plan-reaffirm at {cleaned_new[:12]} by {by}; supersedes base {(latest.base_sha or '<none>')[:12]}"
    if dispositions:
        trail += "; dispositions: " + " | ".join(dispositions)
    trail += f"]\n\n{latest.plan_text}"
    try:
        return PlanArtifact.record(
            ticket=ticket,
            plan_text=trail,
            recorded_by=by,
            base_sha=cleaned_new,
            adequacy=manifest,
        )
    except ValueError as exc:
        # Covers BOTH an inadequate carried-forward manifest (no fresh supplied) and a
        # thin/garbage fresh_adequacy — the fresh-manifest path is not an adequacy bypass.
        msg = (
            f"cannot reaffirm ticket {ticket.pk} into an adequate plan: {exc} The manifest to record "
            f"(carried-forward, or supplied via --adequacy-json) is not adequate. Supply a complete four-section "
            f"manifest with `--adequacy-json '<{{design,integration_seams,edge_cases,test_strategy}}>'`, OR record "
            f"an audited bypass (`ticket plan-bypass {ticket.pk} --human-authorize <who> --reason <why>`), OR "
            f"disable the gate (`config_setting set require_plan_adequacy false --overlay <name>`)."
        )
        raise ReaffirmError(msg) from exc
