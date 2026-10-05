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
    A live ``CodeHostQuery.pr_merge_state`` probe over the ticket's PR refs
    confirms ``MERGED`` and supplies a merge commit SHA. Board and completion
    reconciliation persist that result on the ticket before transitioning; a
    direct transition can still verify it live. An unreadable forge or a merged
    response without a SHA is inconclusive and yields no evidence.

Invoked from the ``Ticket.mark_merged()`` and ``Ticket.reconcile_merged()``
transition bodies exactly as ``ship()`` invokes ``local_e2e_dod`` — the single
chokepoint every path to MERGED funnels through. On a block it raises
:class:`NoMergeEvidenceError`; the transition does not advance.
"""

import logging
import re
from typing import TYPE_CHECKING

from teatree.core.merge.ci_rollup import CodeHostQuery
from teatree.core.modelkit.gate_registry import register_gate
from teatree.core.models import MergeAudit, PullRequest
from teatree.core.models.errors import InvalidTransitionError
from teatree.url_classify import pr_ref
from teatree.utils.forge import forge_from_remote
from teatree.utils.pr_ref import PrRef

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket

logger = logging.getLogger(__name__)
_MERGED_SHA = re.compile(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?\Z")


class NoMergeEvidenceError(InvalidTransitionError):
    """A terminal MERGED transition was refused: the ticket has no merged-SHA evidence.

    A subclass of :class:`InvalidTransitionError` (sibling of
    :class:`~teatree.core.gates.dod_gate.DodLocalE2EError`) so the caller's outer
    atomic rolls the advance back and the FSM stays put. The message names the
    keystone audit or live forge evidence that satisfies the gate.
    """


def has_merge_audit_evidence(ticket: "Ticket") -> bool:
    """True iff a ``MergeAudit`` row with a real (non-blank) ``merged_sha`` is linked to *ticket*."""
    shas = MergeAudit.objects.filter(clear__ticket=ticket).values_list("merged_sha", flat=True)
    return any(sha and sha.strip() for sha in shas)


def _pr_host_kind(pr: PullRequest) -> str:
    return forge_from_remote(pr.url) or "github"


def _probeable_refs(ticket: "Ticket") -> list[PrRef]:
    """Every PR of *ticket* the gate can ask the forge about, rows first.

    A ticket with no ``PullRequest`` row is not a ticket with no PR: the board
    reconcile's rule B targets exactly the ticket whose OWN ``issue_url`` names the
    PR, and that is the shape with no row by definition (#3859). Deriving refs only
    from rows left that queryset empty, so the gate found no evidence and refused the
    very transition the reconcile was holding a definite forge MERGED verdict for.
    The rows stay authoritative; the ``issue_url`` fills the gap only when there are
    none, and only when it names a pull request — an ``/issues/N`` url parses to no
    ref and is never probed.
    """
    refs: list[PrRef] = []
    for pr in PullRequest.objects.filter(ticket=ticket):
        slug = (pr.repo or "").strip()
        raw_id = str(pr.iid or "").strip()
        if not slug or not raw_id.isdigit():
            continue
        refs.append(PrRef(slug=slug, pr_id=int(raw_id), host_kind=_pr_host_kind(pr)))
    if refs:
        return refs
    own = pr_ref(ticket.issue_url or "")
    return [own] if own is not None else []


def forge_confirms_merged(ticket: "Ticket") -> bool:
    """True iff a live forge probe confirms a PR of *ticket* has a merged SHA — FAIL-CLOSED.

    The never-wedge fallback for a genuinely-merged PR whose keystone MergeAudit
    row is absent. Any probe failure (forge unreachable, backend unconfigured,
    malformed response) is inconclusive and yields no evidence, so a transport
    error is never mistaken for "merged".
    """
    for ref in _probeable_refs(ticket):
        try:
            state = CodeHostQuery.for_ref(ref).pr_merge_state()
        except Exception:  # noqa: BLE001 — any probe failure is inconclusive; fail CLOSED (no evidence).
            logger.warning(
                "merge_evidence gate: forge merge-state probe failed for %s#%s; treating as NOT merged (fail-closed).",
                ref.slug,
                ref.pr_id,
            )
            continue
        if state.is_merged and _MERGED_SHA.fullmatch(state.merge_commit_oid.strip()):
            return True
    return False


def record_confirmed_forge_merge(ticket: "Ticket") -> bool:
    """Persist a forge-confirmed merged SHA before a reconcile transition.

    A merged PR row or an issue closure alone contains no commit SHA. Read the
    forge's merge result for a PR actually attached to this ticket, and retain
    its SHA through the ticket's locked evidence writer.
    """
    if has_merge_audit_evidence(ticket) or has_recorded_forge_merge(ticket):
        return True
    for ref in _probeable_refs(ticket):
        try:
            state = CodeHostQuery.for_ref(ref).pr_merge_state()
        except Exception:
            logger.warning(
                "merge_evidence: could not verify the merged SHA for %s#%s", ref.slug, ref.pr_id, exc_info=True
            )
            continue
        sha = state.merge_commit_oid.strip()
        if not state.is_merged or not _MERGED_SHA.fullmatch(sha):
            continue
        ticket.merge_extra(set_keys={"forge_merge_evidence": {"slug": ref.slug, "pr_id": ref.pr_id, "merged_sha": sha}})
        PullRequest.objects.record_forge_merge(slug=ref.slug, pr_id=ref.pr_id)
        return True
    return False


def has_recorded_forge_merge(ticket: "Ticket") -> bool:
    """Accept only a recorder's SHA for a PR still named by this ticket."""
    recorded = (ticket.extra or {}).get("forge_merge_evidence")
    if not isinstance(recorded, dict):
        return False
    sha = str(recorded.get("merged_sha", ""))
    if not _MERGED_SHA.fullmatch(sha):
        return False
    return any(
        ref.slug.casefold() == str(recorded.get("slug", "")).casefold() and ref.pr_id == recorded.get("pr_id")
        for ref in _probeable_refs(ticket)
    )


def has_merge_evidence(ticket: "Ticket") -> bool:
    """True iff an audit, a recorded forge SHA, or a live forge read proves the merge."""
    return has_merge_audit_evidence(ticket) or has_recorded_forge_merge(ticket) or forge_confirms_merged(ticket)


def check_merge_evidence(ticket: "Ticket") -> None:
    """Refuse a terminal MERGED transition unless *ticket* has real merged-SHA evidence.

    Order of short-circuits (cheapest, most-permissive first):

    1. A keystone ``MergeAudit`` row with a real ``merged_sha`` → pass (no network).
    2. A previously recorded forge SHA for this ticket's PR → pass.
    3. A live forge probe confirming a PR's merged SHA → pass (the fallback).
    4. Otherwise → raise :class:`NoMergeEvidenceError`.
    """
    if has_merge_evidence(ticket):
        return
    msg = (
        f"Refusing to mark ticket {ticket} MERGED — it has no merged-SHA evidence. MERGED is "
        f"reachable only with a real merge: a MergeAudit row the merge keystone wrote (the "
        f"sanctioned `t3 <overlay> ticket merge <clear_id>` path), or the forge itself confirming "
        f"the PR merged. A committed-and-tested-but-unpushed ticket is not done. "
        f"Resolve the missing evidence and retry."
    )
    raise NoMergeEvidenceError(msg)


register_gate("merge_evidence", check_merge_evidence)
