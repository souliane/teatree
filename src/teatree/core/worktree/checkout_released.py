"""Recover a released writer's checkout from its exact owned GitHub PR head."""

import logging
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from django.utils import timezone

from teatree.core import forge_pr_probe
from teatree.core.cleanup.process_table import ProcessTable
from teatree.core.modelkit.phase_tools import phase_writes_checkout
from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models import PullRequest, Task, Worktree
from teatree.core.models.checkout_custody import Custody
from teatree.core.models.worktree_occupancy import task_holder_id
from teatree.core.worktree.checkout_recovery import _attest, _holder_task, _process_evidence, _state_unchanged
from teatree.core.worktree.checkout_state import CheckoutState, checkout_digest
from teatree.utils import git
from teatree.utils.forge import forge_from_remote
from teatree.utils.git_remote import host_from_remote
from teatree.utils.run import CommandFailedError
from teatree.utils.url_slug import pr_ref_from_url

if TYPE_CHECKING:
    from teatree.core.models.checkout_proof import ReleasedPrHeadAttestation

logger = logging.getLogger(__name__)
_MAX_REFLOG_ANCHORS = 64


def _clean_anchor_commit(path: str, branch: str, digest: str) -> str | None:
    entries = git.run_strict(
        repo=path, args=["reflog", "show", "--format=%H", f"--max-count={_MAX_REFLOG_ANCHORS}", "HEAD"]
    )
    for commit in entries.splitlines():
        if checkout_digest(commit, branch) == digest:
            return commit
    return None


def _released_custody(task: Task, worktree: Worktree, custody: Custody) -> bool:
    *previous, current = custody.tenures
    claimant = (task_holder_id(task), task.claimed_by_session)
    if (
        current.ended
        or (current.holder, current.session) != claimant
        or (worktree.occupied_by, worktree.occupied_by_session) != claimant
        or any(entry.ended != "released" for entry in previous)
    ):
        return False
    prior_tasks = [_holder_task(entry.holder, task) for entry in previous]
    return bool(
        all(prior is not None and prior.failure_kind != FailureKind.CANCELLED for prior in prior_tasks)
        and any(prior is not None and phase_writes_checkout(prior.phase) for prior in prior_tasks)
    )


def _owned_github_head_url(task: Task, path: str, state: CheckoutState, head: str) -> tuple[str, str, str] | None:
    origin = git.remote_slug(repo=path)
    origin_url = git.remote_url(repo=path)
    origin_host = host_from_remote(origin_url)
    if not origin or not origin_host or forge_from_remote(origin_url) != "github":
        return None
    pr_head = forge_pr_probe.github_open_pr_head(path, state.branch)
    if pr_head is None:
        return None
    pr_url, head_repo, head_branch, forge_head = pr_head
    ref = pr_ref_from_url(pr_url)
    if ref is None or ref.host_kind != "github":
        return None
    identity_matches = (
        ref.slug.casefold() == origin.casefold() and origin_host == (urlsplit(pr_url).hostname or "").lower()
    )
    head_matches = head_repo.casefold() == origin.casefold() and head_branch == state.branch and forge_head == head
    if not identity_matches or not head_matches:
        return None
    if PullRequest.objects.owning_ticket(slug=ref.slug, pr_id=ref.pr_id, pr_url=pr_url) != task.ticket:
        return None
    remote_head = forge_pr_probe.github_exact_branch_head(path, origin, state.branch, origin_host)
    return (pr_url, forge_head, remote_head) if remote_head is not None and remote_head == head else None


def _released_pr_head_proof(
    task: Task, worktree: Worktree, state: CheckoutState, custody: Custody
) -> tuple[str, str, str, str, str] | None:
    path = worktree.worktree_path
    try:
        anchor = _clean_anchor_commit(path, state.branch, custody.anchor)
        if anchor is None:
            return None
        head = git.run_strict(repo=path, args=["rev-parse", "--verify", "HEAD"])
        if checkout_digest(head, state.branch) != state.digest or not git.check(
            repo=path, args=["merge-base", "--is-ancestor", anchor, head]
        ):
            return None
        owned_head = _owned_github_head_url(task, path, state, head)
        if owned_head is None:
            return None
    except (CommandFailedError, OSError, RuntimeError, ValueError) as exc:
        logger.warning("Released PR head proof unreadable for checkout %s: %s", worktree.pk, exc)
        return None
    pr_url, pr_head, remote_head = owned_head
    return anchor, head, pr_url, pr_head, remote_head


def reattest_released_pr_head(task: Task, worktree: Worktree, state: CheckoutState, table: ProcessTable) -> bool:
    custody = Custody.from_extra(worktree.get_extra())
    if custody is None or len(custody.tenures) <= 1 or state.dirty or state.branch != worktree.branch:
        return False
    if (
        not table.usable
        or table.holds(Path(worktree.worktree_path))
        or table.eviction_refuse_reason(nested_writer=custody.prior_tenures_nested)
        or not _released_custody(task, worktree, custody)
    ):
        return False
    proof = _released_pr_head_proof(task, worktree, state, custody)
    if proof is None or not _state_unchanged(task, worktree, state):
        return False
    anchor, head, pr_url, pr_head, remote_head = proof
    current = custody.tenures[-1]
    audit: ReleasedPrHeadAttestation = {
        "at": timezone.now().isoformat(),
        "previous": custody.anchor,
        "digest": state.digest,
        "anchor_commit": anchor,
        "current_head": head,
        "pr_head": pr_head,
        "remote_head": remote_head,
        "pull_request": pr_url,
        "tenures": [entry.as_dict() for entry in custody.tenures],
        "process_table": _process_evidence(table, custody),
        "holder": {"id": current.holder, "session": current.session},
    }
    attested = _attest(worktree, custody, state.digest, audit)
    if attested:
        logger.info("Re-attested released PR-head checkout %s at %s", worktree.pk, head)
    return attested
