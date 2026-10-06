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
ratchet). :func:`evaluate_debt_delta` is the shared diff+policy orchestration
wired at every core ``host.create_pr`` seam so no route bypasses it (mirroring the
PR-2 budget gate): the ship pipeline chokepoint
``ShipExecutor._refusal_before_outward_write`` (both the interactive ``pr create``
async worker AND the autonomous loop's task-driven ship converge there), the
``_run_ship_gates`` pre-push fail-fast (the interactive ``pr create`` path), and
``_ensure_pr.create_or_defer_pr`` (the orphan-branch path). All of them conclude
before the push, never after it (#4151).
"""

from teatree.core.gates.plan_currency_gate import latest_plan_artifact
from teatree.core.modelkit.gate_verdict import Pass, Refuse, Verdict, evaluate_gate
from teatree.core.models import Ticket
from teatree.quality.debt_delta import DebtIntroduction, DebtWaiver, load_debt_waivers, scan_debt_delta, unwaived_debt
from teatree.utils import git


class DebtDeltaExceededError(RuntimeError):
    """Refusal raised when a ship diff introduces unwaived net-new tech debt."""


def evaluate_debt_delta(ticket: Ticket, repo_path: str) -> str | None:
    """The single diff+policy orchestration both PR-creation seams share.

    Diffs merge-base..HEAD in *repo_path* (the shrink-only
    delta source) and runs :func:`check_debt_delta`. Returns the rendered refusal on
    unwaived net-new debt AND when the diff cannot be read (no real repo, git error),
    since a diff nobody read proves nothing about its debt; ``None`` only when the
    diff was read and is clean. The three call sites
    (``ShipExecutor._refusal_before_outward_write``, ``_ship_gates.run_debt_delta_gate``,
    and ``_ensure_pr.create_or_defer_pr``) wrap this into their own result shape.
    """
    result = evaluate_gate(
        "debt_delta",
        collect=lambda: git.branch_diff(repo=repo_path),
        judge=lambda diff: _debt_verdict(ticket, diff),
    )
    return None if result.passed else result.render()


def _debt_verdict(ticket: Ticket, diff_text: str) -> Verdict:
    try:
        check_debt_delta(ticket, diff_text)
    except DebtDeltaExceededError as exc:
        return Refuse(str(exc))
    return Pass()


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
    raise DebtDeltaExceededError(_refusal_message(remaining))


def _refusal_message(introductions: list[DebtIntroduction]) -> str:
    offending = "\n".join(f"  - [{intro.kind}] {intro.path}: {intro.line}" for intro in introductions)
    return (
        f"debt_delta_gate: this ship introduces {len(introductions)} net-new tech-debt "
        f"suppression(s) with no plan-manifest waiver:\n{offending}\n"
        f"Remove the suppression(s) or record an `approved_debt` waiver (pattern + reason) in the plan manifest."
    )
