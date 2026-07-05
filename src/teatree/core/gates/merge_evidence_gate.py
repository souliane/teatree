"""merge_evidence FSM gate: MERGED is unreachable without real merged-SHA evidence.

The recurrence this forecloses: a ticket that was committed and tested but never
pushed/merged was walked to the terminal ``MERGED`` state and believed done —
"believe work is done when it's not" at the FSM root. The ``mark_merged()`` /
``reconcile_merged()`` transition bodies carried ZERO evidence conditions, so the
ungated ``_advance_ticket`` walk (``ticket.py``) and the loop's
``complete_ticket`` action could reach MERGED with unpushed, unmerged work. A
remembered "verify it actually merged" rule did not hold; this gate is the
deterministic substitute.

The evidence is a DB row or the forge's own word — there is no free-text field:

Keystone artifact (pure, no network)
    A ``MergeAudit`` row with a real (non-blank) ``merged_sha`` linked to the
    ticket's ``MergeClear``. The merge keystone
    (``merge.execution.record_merge_and_advance``) writes this row atomically
    BEFORE calling ``reconcile_merged()`` in the same transaction, so a real
    keystone merge always satisfies the gate without a forge call — the normal
    merge path is never over-blocked.

Forge fallback (live, fail-closed — the never-wedge escape)
    A live ``fetch_pr_merge_state`` probe over the ticket's ``PullRequest`` rows
    that confirms a PR is ``MERGED``. This covers the genuinely-merged PR whose
    keystone MergeAudit row is absent (a manual / out-of-band merge), so a real
    merge is never falsely wedged. An unreachable or erroring probe is
    INCONCLUSIVE and yields no evidence: believe-done-not-done is exactly the
    failure this gate kills, so an indeterminate probe never passes on error.

Kill-switch (documented never-lockout escape)
    ``require_merge_evidence`` (per-overlay overridable, DB-first) is off by
    default and enabled for the teatree overlay so it bites real teatree
    tickets. Setting it back off is the operator's audited escape if a forge
    outage would otherwise wedge a ticket the forge cannot confirm.

Invoked from the ``Ticket.mark_merged()`` and ``Ticket.reconcile_merged()``
transition bodies exactly as ``ship()`` invokes ``local_e2e_dod`` — the single
chokepoint every path to MERGED funnels through. On a block it raises
:class:`NoMergeEvidenceError`; the transition does not advance.
"""

import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from teatree.config import get_effective_settings
from teatree.core.merge.ci_rollup import fetch_pr_merge_state
from teatree.core.modelkit.gate_registry import register_gate
from teatree.core.models import MergeAudit, PullRequest
from teatree.core.models.errors import InvalidTransitionError

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket

logger = logging.getLogger(__name__)


class NoMergeEvidenceError(InvalidTransitionError):
    """A terminal MERGED transition was refused: the ticket has no merged-SHA evidence.

    A subclass of :class:`InvalidTransitionError` (sibling of
    :class:`~teatree.core.gates.dod_gate.DodLocalE2EError`) so the caller's outer
    atomic rolls the advance back and the FSM stays put. The message names the
    kill-switch escape so a forge-down false-negative can never permanently wedge
    a legitimately-merged ticket.
    """


def merge_evidence_required(overlay_name: str | None) -> bool:
    """Whether the merge-evidence gate is in force for *overlay_name* (overlay -> global)."""
    return bool(get_effective_settings(overlay_name).require_merge_evidence)


def has_merge_audit_evidence(ticket: "Ticket") -> bool:
    """True iff a ``MergeAudit`` row with a real (non-blank) ``merged_sha`` is linked to *ticket*."""
    shas = MergeAudit.objects.filter(clear__ticket=ticket).values_list("merged_sha", flat=True)
    return any(sha and sha.strip() for sha in shas)


def _pr_host_kind(pr: PullRequest) -> str:
    host = (urlparse(pr.url).hostname or "").lower()
    return "gitlab" if "gitlab" in host else "github"


def forge_confirms_merged(ticket: "Ticket") -> bool:
    """True iff a live forge probe confirms a PR of *ticket* is MERGED — FAIL-CLOSED.

    The never-wedge fallback for a genuinely-merged PR whose keystone MergeAudit
    row is absent. Any probe failure (forge unreachable, backend unconfigured,
    malformed response) is inconclusive and yields no evidence, so a transport
    error is never mistaken for "merged".
    """
    for pr in PullRequest.objects.filter(ticket=ticket):
        slug = (pr.repo or "").strip()
        raw_id = str(pr.iid or "").strip()
        if not slug or not raw_id.isdigit():
            continue
        try:
            state = fetch_pr_merge_state(slug, int(raw_id), host_kind=_pr_host_kind(pr))
        except Exception:  # noqa: BLE001 — any probe failure is inconclusive; fail CLOSED (no evidence).
            logger.warning(
                "merge_evidence gate: forge merge-state probe failed for %s#%s; treating as NOT merged (fail-closed).",
                slug,
                raw_id,
            )
            continue
        if state.is_merged:
            return True
    return False


def has_merge_evidence(ticket: "Ticket") -> bool:
    """True iff *ticket* has real merged-SHA evidence: a keystone MergeAudit row OR a live forge MERGED confirmation."""
    return has_merge_audit_evidence(ticket) or forge_confirms_merged(ticket)


def check_merge_evidence(ticket: "Ticket") -> None:
    """Refuse a terminal MERGED transition unless *ticket* has real merged-SHA evidence.

    Order of short-circuits (cheapest, most-permissive first):

    1. Gate off (``require_merge_evidence`` unset for the overlay) → pass.
    2. A keystone ``MergeAudit`` row with a real ``merged_sha`` → pass (no network).
    3. A live forge probe confirming a PR is MERGED → pass (the never-wedge fallback).
    4. Otherwise → raise :class:`NoMergeEvidenceError`.
    """
    if not merge_evidence_required(ticket.overlay or None):
        return
    if has_merge_evidence(ticket):
        return
    msg = (
        f"Refusing to mark ticket {ticket} MERGED — it has no merged-SHA evidence. MERGED is "
        f"reachable only with a real merge: a MergeAudit row the merge keystone wrote (the "
        f"sanctioned `t3 <overlay> ticket merge <clear_id>` path), or the forge itself confirming "
        f"the PR merged. A committed-and-tested-but-unpushed ticket is NOT done. If a genuinely "
        f"merged PR cannot be confirmed (forge outage), the operator's audited escape is to disable "
        f"the gate: `t3 <overlay> config_setting set require_merge_evidence false --overlay <name>`."
    )
    raise NoMergeEvidenceError(msg)


register_gate("merge_evidence", check_merge_evidence)
