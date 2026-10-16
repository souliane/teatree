"""Re-attest a stranded checkout when its custody and host state prove it safe."""

import logging
from pathlib import Path

from django.utils import timezone

from teatree.core.cleanup.process_table import ProcessTable
from teatree.core.modelkit.phase_tools import MCP_WRITE, tools_for_phase
from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models import Task, Worktree
from teatree.core.models.checkout_custody import Custody, Tenure
from teatree.core.models.checkout_proof import AutoAttestation, DeferredExitAttestation
from teatree.core.models.types import WorktreeExtra
from teatree.core.models.worktree_occupancy import task_holder_id
from teatree.core.worktree.checkout_state import CheckoutState, checkout_state

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


def _expected_branches(task: Task, worktree: Worktree, state: CheckoutState) -> dict[str, str] | None:
    branches: dict[str, str] = {}
    for row in Worktree.objects.filter(ticket=task.ticket).exclude(extra__worktree_path__isnull=True):
        if not row.worktree_path or not row.branch:
            return None
        try:
            observed = state if row.pk == worktree.pk else checkout_state(Path(row.worktree_path), ticket=task.ticket)
        except (OSError, RuntimeError, ValueError):
            return None
        if observed.branch != row.branch or observed.branch == "HEAD":
            return None
        branches[row.worktree_path] = observed.branch
    return branches if worktree.worktree_path in branches else None


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
    branches = _expected_branches(task, worktree, state) if prior is not None else None
    if prior is None or branches is None or not _state_unchanged(task, worktree, state):
        return False
    current: Tenure = custody.tenures[-1]
    audit: AutoAttestation = {
        "at": timezone.now().isoformat(),
        "previous": custody.anchor,
        "digest": state.digest,
        "tenures": [entry.as_dict() for entry in custody.tenures],
        "process_table": {"source": table.source, "gaps": list(table.gaps)},
        "branches": branches,
        "holder": {"id": current.holder, "session": current.session},
    }
    if not _attest(worktree, custody, state.digest, audit):
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
    if not _state_unchanged(task, worktree, state):
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
    attested = _attest(worktree, custody, state.digest, audit)
    if attested:
        logger.info("Attested deferred exit for checkout %s after %s", worktree.pk, previous.holder)
    return attested


def _attest(
    worktree: Worktree, custody: Custody, digest: str, audit: AutoAttestation | DeferredExitAttestation
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
