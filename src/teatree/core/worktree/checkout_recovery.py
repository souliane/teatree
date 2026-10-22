"""Re-attest a stranded checkout when its custody and host state prove it safe."""

import logging
from pathlib import Path

from django.utils import timezone

from teatree.core import forge_pr_probe
from teatree.core.cleanup.process_table import ProcessTable
from teatree.core.modelkit.phase_tools import phase_writes_checkout
from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models import PullRequest, Task, Worktree
from teatree.core.models.checkout_custody import Custody, Tenure
from teatree.core.models.checkout_proof import (
    AutoAttestation,
    CheckoutProcessEvidence,
    DeferredExitAttestation,
    ReleasedPrHeadAttestation,
)
from teatree.core.models.types import WorktreeExtra
from teatree.core.models.worktree_occupancy import _task_pk_from_holder, task_holder_id
from teatree.core.worktree.checkout_state import CheckoutState, checkout_state
from teatree.utils.pr_ref import PrRef
from teatree.utils.url_slug import pr_ref_from_url

logger = logging.getLogger(__name__)


def _holder_task_for_ticket(holder: str, task: Task) -> Task | None:
    pk = _task_pk_from_holder(holder)
    return Task.objects.filter(pk=pk, ticket=task.ticket).first() if pk is not None else None


def _failed_cancelled(task: Task | None) -> bool:
    return task is not None and task.status == Task.Status.FAILED and task.failure_kind == FailureKind.CANCELLED


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
    prior = _holder_task_for_ticket(prior_holder, task)
    if prior is None or not phase_writes_checkout(prior.phase) or _failed_cancelled(prior):
        return None
    return prior


def _owned_pr_url(task: Task, row: Worktree, branch: str) -> str:
    probe = forge_pr_probe.find_open_pr_for_branch(row.worktree_path, branch)
    ref = pr_ref_from_url(probe.url) if probe.is_found else None
    return probe.url if ref is not None and pr_owned_by_ticket(task, ref, probe.url) else ""


def pr_owned_by_ticket(task: Task, ref: PrRef, url: str) -> bool:
    return PullRequest.objects.owning_ticket(slug=ref.slug, pr_id=ref.pr_id, pr_url=url) == task.ticket


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


def checkout_state_unchanged(task: Task, worktree: Worktree, state: CheckoutState) -> bool:
    try:
        confirmed = checkout_state(Path(worktree.worktree_path), ticket=task.ticket)
    except (OSError, RuntimeError, ValueError):
        return False
    return confirmed.digest == state.digest


def checkout_process_evidence(table: ProcessTable, custody: Custody) -> CheckoutProcessEvidence:
    evidence: CheckoutProcessEvidence = {"source": table.source, "gaps": list(table.gaps)}
    if custody.prior_tenures_nested and table.host_namespace_gaps:
        evidence["set_aside"] = list(table.host_namespace_gaps)
    return evidence


def reattest_on_proof(task: Task, worktree: Worktree, state: CheckoutState, table: ProcessTable) -> bool:
    custody = Custody.from_extra(worktree.get_extra())
    if (
        custody is None
        or table.eviction_refuse_reason(nested_writer=custody.prior_tenures_nested)
        or table.holds(Path(worktree.worktree_path))
    ):
        return False
    prior = _interrupted_writer(task, custody)
    branch_proof = _expected_branches(task, worktree, state) if prior is not None else None
    if prior is None or branch_proof is None or not checkout_state_unchanged(task, worktree, state):
        return False
    branches, pull_requests = branch_proof
    current: Tenure = custody.tenures[-1]
    audit: AutoAttestation = {
        "at": timezone.now().isoformat(),
        "previous": custody.anchor,
        "digest": state.digest,
        "tenures": [entry.as_dict() for entry in custody.tenures],
        "process_table": checkout_process_evidence(table, custody),
        "branches": branches,
        "pull_requests": pull_requests,
        "holder": {"id": current.holder, "session": current.session},
    }
    if not persist_checkout_recovery_attestation(worktree, custody, state.digest, audit):
        return False
    logger.info("Re-attested checkout %s after task %s", worktree.pk, prior.pk)
    return True


def attest_deferred_exit(task: Task, worktree: Worktree, state: CheckoutState, table: ProcessTable) -> bool:
    extra = worktree.get_extra()
    record = extra.get("deferred_exit")
    custody = Custody.from_extra(extra)
    if not isinstance(record, dict) or any(
        not isinstance(record.get(key), str) for key in ("holder", "session", "digest", "cause", "at")
    ):
        return False
    if record["cause"] not in {"own_processes", "process_table"} or custody is None or len(custody.tenures) <= 1:
        return False
    if not table.usable or table.holds(Path(worktree.worktree_path)) or record["digest"] != state.digest:
        return False
    previous, current = custody.tenures[-2:]
    claimant = (task_holder_id(task), task.claimed_by_session)
    if (
        not previous.ended
        or (previous.holder, previous.session) != (record["holder"], record["session"])
        or current.ended
        or (current.holder, current.session) != claimant
        or (worktree.occupied_by, worktree.occupied_by_session) != claimant
    ):
        return False
    if _failed_cancelled(_holder_task_for_ticket(record["holder"], task)) or not checkout_state_unchanged(
        task, worktree, state
    ):
        return False
    audit: DeferredExitAttestation = {
        "at": timezone.now().isoformat(),
        "previous": custody.anchor,
        "digest": state.digest,
        "tenures": [entry.as_dict() for entry in custody.tenures],
        "process_table": {"source": table.source, "gaps": [*table.gaps, *table.eviction_gaps]},
        "holder": {"id": current.holder, "session": current.session},
        "deferred_exit": record,
    }
    attested = persist_checkout_recovery_attestation(worktree, custody, state.digest, audit)
    if attested:
        logger.info("Attested deferred exit for checkout %s after %s", worktree.pk, previous.holder)
    return attested


def persist_checkout_recovery_attestation(
    worktree: Worktree,
    custody: Custody,
    digest: str,
    audit: AutoAttestation | DeferredExitAttestation | ReleasedPrHeadAttestation,
) -> bool:
    current = custody.tenures[-1]
    worktree.merge_extra(
        set_keys=WorktreeExtra(
            attested_state=digest,
            checkout_custody=Custody.reanchored(digest, custody, current.holder, current.session).as_dict(),
            auto_attestation=audit,
        )
    )
    saved = Worktree.objects.get(pk=worktree.pk).get_extra()
    return saved.get("attested_state") == digest and saved.get("auto_attestation") == audit
