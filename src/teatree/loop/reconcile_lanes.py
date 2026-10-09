"""Pre-scan ledger reconciliation — ask the forge before a lane reads its own rows.

Both adapters here answer the same shape of question. A durable row only ever learns
its PR landed from whoever wrote it, so one settled out of band (a hand-run merge, a
lost post-hook) reads live forever and every surface built on it answers wrong about
the same PR. Each lane therefore reconciles its rows against the forge before it reads
them.

Split out of ``scanner_factories`` (whose concern is building scanners, and which sits
at its module-health LOC cap), and kept out of ``core`` because ``core`` must not
import a backend. Both bind ``pr_open_state``, whose every failure collapses to
UNKNOWN — which settles nothing — so both are best-effort: a forge outage leaves the
ledger as recorded and never stops the tick.
"""

import logging

logger = logging.getLogger(__name__)


def reconcile_holder_pr_rows_best_effort(overlay_name: str) -> None:
    """Ask the forge about each budget holder's PR before the budget is read (#3984).

    Both intake readings — the release rule and the deadlock alarm — are drawn from
    ``PullRequest.state``, so a row nobody advanced after its PR merged holds the slot
    AND silences the alarm about it.
    """
    from teatree.backends.loader import pr_open_state  # noqa: PLC0415 — deferred: loaded at tick time
    from teatree.core.intake.budget import reconcile_holder_pr_rows  # noqa: PLC0415 — leaf import

    try:
        reconcile_holder_pr_rows(overlay_name, read_state=pr_open_state)
    except Exception:
        logger.exception(
            "intake: could not reconcile held PR rows for %s — reading the budget as recorded", overlay_name
        )


def reconcile_settled_clears_best_effort() -> None:
    """Consume every standing CLEAR whose PR already merged or closed, global scope (#4250).

    Unattended convergence: without it a CLEAR orphaned by a lost post-hook stands
    forever and every backlog surface keeps reporting it, so the operator alarm's
    steady state is a list of PRs that already merged. A CLEAR whose repo no overlay
    declares is in the same backlog, hence the global scope.
    """
    from django.utils import timezone  # noqa: PLC0415 — deferred: loaded at tick time

    from teatree.backends.loader import pr_open_state  # noqa: PLC0415 — deferred: loaded at tick time
    from teatree.core.merge.clear_reconcile import reconcile_settled_clears  # noqa: PLC0415 — leaf import

    try:
        reconcile_settled_clears(read_state=pr_open_state, now=timezone.now())
    except Exception:
        logger.exception("pr_sweep: could not reconcile settled merge CLEARs — leaving the backlog as recorded")
