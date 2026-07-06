"""The deterministic no-new-tech-debt MERGE gate (north-star PR-3).

Mirrors the ``architecture_precheck`` (pure scanner in :mod:`teatree.quality`) +
``architecture_precheck_gate`` (thin core wrapper) split: the pure diff scanner
and waiver logic live in :mod:`teatree.quality.debt_delta`; this gate is the
DB-touching wrapper the ship chain calls. It scans a ship diff for net-new debt
suppressions and refuses unless every introduction is covered by an audited
``approved_debt`` waiver on the ticket's latest plan manifest — mechanizing
CLAUDE.md's "no tech debt without explicit approval" as a recorded artifact,
never a silent bypass.

The gate is delta-only (added lines) so legacy debt is exempt (a shrink-only
ratchet) and ships DARK behind ``require_debt_delta``: inert until an overlay
opts in. :func:`evaluate_debt_delta` is the shared flag+diff+policy orchestration
wired at every core ``host.create_pr`` seam so no route bypasses it (mirroring the
PR-2 budget gate): the ship pipeline chokepoint ``ShipExecutor._open_pr_and_record``
(both the interactive ``pr create`` async worker AND the autonomous loop's
task-driven ship converge there), the ``_run_ship_gates`` pre-push fail-fast (the
interactive ``pr create`` path), and ``_ensure_pr.create_or_defer_pr`` (the
orphan-branch path).
"""

from teatree.config import get_effective_settings
from teatree.core.gates.plan_currency_gate import latest_plan_artifact
from teatree.core.models import Ticket
from teatree.quality.debt_delta import DebtIntroduction, DebtWaiver, load_debt_waivers, scan_debt_delta, unwaived_debt
from teatree.utils import git
from teatree.utils.run import CommandFailedError


class DebtDeltaExceededError(RuntimeError):
    """Refusal raised when a ship diff introduces unwaived net-new tech debt."""


def evaluate_debt_delta(ticket: Ticket, repo_path: str) -> str | None:
    """The single flag+diff+policy orchestration both PR-creation seams share.

    DARK-gated by ``require_debt_delta`` (resolved per overlay), so a no-op unless
    the overlay opts in. Diffs merge-base..HEAD in *repo_path* (the shrink-only
    delta source) and runs :func:`check_debt_delta`. Returns the refusal message on
    unwaived net-new debt, or ``None`` when clean, inert, or unverifiable (no real
    repo / git error) — mirroring the branch-currency / mandatory-E2E posture. The
    three call sites (``ShipExecutor._open_pr_and_record``,
    ``_ship_gates.run_debt_delta_gate``, and ``_ensure_pr.create_or_defer_pr``)
    wrap this into their own result shape.
    """
    if not get_effective_settings(ticket.overlay or None).require_debt_delta:
        return None
    try:
        diff = git.branch_diff(repo=repo_path)
    except (CommandFailedError, RuntimeError, ValueError):
        return None
    try:
        check_debt_delta(ticket, diff)
    except DebtDeltaExceededError as exc:
        return str(exc)
    return None


def waivers_for_ticket(ticket: Ticket) -> tuple[DebtWaiver, ...]:
    """The ``approved_debt`` waivers on *ticket*'s latest plan manifest, or ``()``."""
    artifact = latest_plan_artifact(ticket)
    if artifact is None:
        return ()
    return load_debt_waivers(artifact.adequacy)


def check_debt_delta(ticket: Ticket, diff_text: str, *, waivers: tuple[DebtWaiver, ...] | None = None) -> None:
    """Refuse *diff_text* if it introduces net-new debt no waiver covers.

    *waivers* defaults to the ticket's latest plan-manifest ``approved_debt``
    entries; passing it explicitly is the test seam (and skips the DB read). A
    clean or shrink-only diff returns immediately.
    """
    introductions = scan_debt_delta(diff_text)
    if not introductions:
        return
    resolved = waivers if waivers is not None else waivers_for_ticket(ticket)
    remaining = unwaived_debt(introductions, resolved)
    if not remaining:
        return
    raise DebtDeltaExceededError(_refusal_message(ticket, remaining))


def _refusal_message(ticket: Ticket, introductions: list[DebtIntroduction]) -> str:
    overlay = ticket.overlay or "<overlay>"
    offending = "\n".join(f"  - [{intro.kind}] {intro.path}: {intro.line}" for intro in introductions)
    return (
        f"debt_delta_gate: this ship introduces {len(introductions)} net-new tech-debt "
        f"suppression(s) with no plan-manifest waiver:\n{offending}\n"
        f"Remove the suppression(s), record an `approved_debt` waiver (pattern + reason) in the "
        f"plan manifest, or lift the gate: "
        f"`t3 {overlay} config_setting set require_debt_delta false --overlay {overlay}`."
    )
