"""Factory decision for a checkout whose prior attestation no longer matches."""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from django.utils import timezone

from teatree.core.cleanup.cleanup_liveness import _git_lock_present
from teatree.core.cleanup.process_table import ProcessTable, read_process_table
from teatree.core.cleanup.unshipped_work import (
    COMMITS_SUFFIX,
    bundle_path,
    capture_unshipped_work,
    probe_unshipped_work,
)
from teatree.core.forge_push_refs import head_is_detached, operation_in_progress
from teatree.core.models import Task, Worktree
from teatree.core.models.checkout_custody import Custody
from teatree.core.models.worktree_occupancy import occupancy_holder, task_holder_id
from teatree.core.worktree.checkout_recovery import checkout_state_unchanged, interrupted_writer
from teatree.core.worktree.checkout_released import owned_github_pr_url
from teatree.utils import git

if TYPE_CHECKING:
    from teatree.core.models.checkout_proof import SafeCheckoutAttestation
    from teatree.core.worktree.checkout_state import CheckoutState


@dataclass(frozen=True)
class _ProcessProof:
    source: Literal["host", "venue"]
    table: ProcessTable


@dataclass(frozen=True)
class _Preservation:
    commits: int
    artifact_prefix: str


def _checkout_obstacle(path: Path, state: "CheckoutState") -> str:
    if state.uncommitted:
        return f"checkout {path} holds {state.uncommitted} uncommitted file(s) the factory did not record"
    if operation := operation_in_progress(str(path)):
        return f"checkout {path} has {operation} in progress"
    if head_is_detached(str(path)):
        return f"checkout {path} has detached HEAD"
    lock = _git_lock_present(path)
    if lock.fired or lock.unanswered:
        return f"checkout {path} has an index lock or its gitdir could not be checked"
    return ""


def _process_proof(task: Task, worktree: Worktree, table: ProcessTable) -> _ProcessProof | str:
    if table.usable and not table.eviction_gaps:
        return _ProcessProof("host", table)
    path = Path(worktree.worktree_path)
    venue = read_process_table(exclude_ancestors=True, venue_only=True)
    if not venue.usable or venue.eviction_gaps:
        return f"checkout {path} process table unavailable: {venue.refuse_reason()}"
    if venue.holds(path):
        return f"checkout {path} has a live process in this venue"
    holder = occupancy_holder(worktree)
    if holder is None or (holder.holder, holder.holder_session) != (task_holder_id(task), task.claimed_by_session):
        return f"checkout {path} has no exclusive claimant lease"
    if any(
        occupancy_holder(row) is not None for row in Worktree.objects.filter(ticket=task.ticket).exclude(pk=worktree.pk)
    ):
        return f"checkout {path} has another live checkout holder"
    return _ProcessProof("venue", venue)


def _preserve_unpushed(task: Task, worktree: Worktree, branch: str) -> _Preservation | str:
    path = Path(worktree.worktree_path)
    observed = probe_unshipped_work(path)
    if observed.unreadable:
        return f"checkout {path} unshipped work could not be read: {observed.unreadable}"
    if not observed.unpushed_commits:
        return _Preservation(0, "")
    custody = Custody.from_extra(worktree.get_extra())
    if custody is None or interrupted_writer(task, custody) is None:
        return f"checkout {path} has unpushed commits without a recorded ticket writer"
    record = capture_unshipped_work(path, branch=branch, overlay=task.ticket.overlay)
    if (
        record is None
        or record.unreadable
        or record.unpushed_commits != observed.unpushed_commits
        or not bundle_path(record.artifact_prefix, COMMITS_SUFFIX).is_file()
        or bundle_path(record.artifact_prefix, COMMITS_SUFFIX).read_text() != observed.commits_patch
    ):
        return f"checkout {path} unpushed commits could not be preserved"
    return _Preservation(len(observed.unpushed_commits), record.artifact_prefix)


def _head_obstacle(task: Task, worktree: Worktree, state: "CheckoutState", preserved: _Preservation) -> str:
    path = Path(worktree.worktree_path)
    if state.commits_ahead and not preserved.commits:
        try:
            head = git.run_strict(repo=str(path), args=["rev-parse", "--verify", "HEAD"])
            if owned_github_pr_url(task, str(path), state, head, allow_ancestor=True) is None:
                return f"checkout {path} has no owned pull request proving its pushed head"
        except (OSError, RuntimeError, ValueError):
            return f"checkout {path} could not verify its pushed head"
    if not checkout_state_unchanged(task, worktree, state):
        return f"checkout {path} changed during safety verification"
    return ""


def auto_attest_safe_checkout(task: Task, worktree: Worktree, state: "CheckoutState", table: ProcessTable) -> str:
    path = Path(worktree.worktree_path)
    if obstacle := _checkout_obstacle(path, state):
        return obstacle
    proof = _process_proof(task, worktree, table)
    if isinstance(proof, str):
        return proof
    preserved = _preserve_unpushed(task, worktree, state.branch)
    if isinstance(preserved, str):
        return preserved
    if obstacle := _head_obstacle(task, worktree, state, preserved):
        return obstacle

    audit: SafeCheckoutAttestation = {
        "at": timezone.now().isoformat(),
        "digest": state.digest,
        "branch": state.branch,
        "process_evidence": proof.source,
        "process_table": {"source": proof.table.source, "gaps": list(proof.table.gaps)},
        "holder": {"id": task_holder_id(task), "session": task.claimed_by_session},
        "preserved": {"commits": preserved.commits, "artifact_prefix": preserved.artifact_prefix},
    }
    worktree.merge_extra(set_keys={"attested_state": state.digest, "auto_attestation": audit})
    saved = Worktree.objects.get(pk=worktree.pk).get_extra()
    if saved.get("attested_state") != state.digest or saved.get("auto_attestation") != audit:
        return f"checkout {path} attestation could not be confirmed"
    return ""
