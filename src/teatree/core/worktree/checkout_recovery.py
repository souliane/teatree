"""Re-attest a stranded checkout when its custody and host state prove it safe."""

import logging
from pathlib import Path

from django.utils import timezone

from teatree.core import forge_pr_probe
from teatree.core.cleanup.process_table import ProcessTable
from teatree.core.modelkit.phase_tools import MCP_WRITE, tools_for_phase
from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models import PullRequest, Task, Worktree
from teatree.core.models.checkout_custody import Custody, Tenure
from teatree.core.models.types import AutoAttestation, WorktreeExtra
from teatree.core.models.worktree_occupancy import task_holder_id
from teatree.core.worktree.checkout_state import CheckoutState, checkout_state
from teatree.utils.url_slug import pr_ref_from_url

logger = logging.getLogger(__name__)


def _interrupted_writer(task: Task, custody: Custody) -> Task | None:
    if not custody.tenures:
        return None
    current = custody.tenures[-1]
    previous = custody.tenures[:-1]
    identities = {(entry.holder, entry.session) for entry in previous}
    if (
        current.ended
        or (current.holder, current.session) != (task_holder_id(task), task.claimed_by_session)
        or not previous
        or any(not entry.ended for entry in previous)
        or len(identities) != 1
    ):
        return None
    prior_holder, _ = next(iter(identities))
    if not prior_holder.startswith("task:") or not prior_holder[5:].isdigit():
        return None
    prior = Task.objects.filter(pk=int(prior_holder[5:]), ticket=task.ticket).first()
    if (
        prior is None
        or not tools_for_phase(prior.phase) & {"write_file", "edit_file", MCP_WRITE}
        or (prior.status == Task.Status.FAILED and prior.failure_kind == FailureKind.CANCELLED)
    ):
        return None
    return prior


def _owned_pr_url(task: Task, row: Worktree, branch: str) -> str:
    probe = forge_pr_probe.find_open_pr_for_branch(row.worktree_path, branch)
    ref = pr_ref_from_url(probe.url) if probe.is_found else None
    if ref is None:
        return ""
    owner = PullRequest.objects.owning_ticket(slug=ref.slug, pr_id=ref.pr_id, pr_url=probe.url)
    return probe.url if owner == task.ticket else ""


def _expected_branches(
    task: Task, worktree: Worktree, state: CheckoutState
) -> tuple[dict[str, str], dict[str, str]] | None:
    branches: dict[str, str] = {}
    pull_requests: dict[str, str] = {}
    for row in Worktree.objects.filter(ticket=task.ticket).exclude(extra__worktree_path__isnull=True):
        if not row.worktree_path or not row.branch:
            return None
        try:
            observed = state if row.pk == worktree.pk else checkout_state(Path(row.worktree_path), ticket=task.ticket)
        except (OSError, RuntimeError, ValueError):
            return None
        if observed.branch == "HEAD":
            return None
        if observed.branch != row.branch:
            pr_url = _owned_pr_url(task, row, observed.branch)
            if not pr_url:
                return None
            pull_requests[row.worktree_path] = pr_url
        branches[row.worktree_path] = observed.branch
    return (branches, pull_requests) if worktree.worktree_path in branches else None


def _state_unchanged(task: Task, worktree: Worktree, state: CheckoutState) -> bool:
    try:
        confirmed = checkout_state(Path(worktree.worktree_path), ticket=task.ticket)
    except (OSError, RuntimeError, ValueError):
        return False
    return confirmed.digest == state.digest


def reattest_on_proof(task: Task, worktree: Worktree, state: CheckoutState, table: ProcessTable) -> bool:
    custody = Custody.from_extra(worktree.get_extra())
    if custody is None or table.eviction_refuse_reason() or table.holds(Path(worktree.worktree_path)):
        return False
    prior = _interrupted_writer(task, custody)
    branch_proof = _expected_branches(task, worktree, state) if prior is not None else None
    if prior is None or branch_proof is None or not _state_unchanged(task, worktree, state):
        return False
    branches, pull_requests = branch_proof
    current: Tenure = custody.tenures[-1]
    audit: AutoAttestation = {
        "at": timezone.now().isoformat(),
        "previous": custody.anchor,
        "digest": state.digest,
        "tenures": [entry.as_dict() for entry in custody.tenures],
        "process_table": {"source": table.source, "gaps": list(table.gaps)},
        "branches": branches,
        "pull_requests": pull_requests,
        "holder": {"id": current.holder, "session": current.session},
    }
    worktree.merge_extra(
        set_keys=WorktreeExtra(
            attested_state=state.digest,
            checkout_custody=Custody.reanchored(state.digest, custody, current.holder, current.session).as_dict(),
            auto_attestation=audit,
        )
    )
    saved = Worktree.objects.get(pk=worktree.pk).get_extra()
    if saved.get("attested_state") != state.digest or saved.get("auto_attestation") != audit:
        return False
    logger.info("Re-attested checkout %s after task %s", worktree.pk, prior.pk)
    return True
