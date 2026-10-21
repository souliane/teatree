"""Recover a released writer's checkout from its exact owned GitHub PR head."""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import urlsplit

from django.utils import timezone

from teatree.core import forge_pr_probe
from teatree.core.cleanup.process_table import ProcessTable
from teatree.core.modelkit.phase_tools import phase_writes_checkout
from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models import Task, Worktree
from teatree.core.models.checkout_custody import RELEASED_EXIT, Custody
from teatree.core.models.worktree_occupancy import task_holder_id
from teatree.core.worktree.checkout_recovery import (
    checkout_process_evidence,
    checkout_state_unchanged,
    persist_checkout_recovery_attestation,
    pr_owned_by_ticket,
    task_id_from_holder,
)
from teatree.core.worktree.checkout_state import CheckoutState, checkout_digest
from teatree.utils import git
from teatree.utils.forge import forge_from_remote
from teatree.utils.git_remote import host_from_remote
from teatree.utils.url_slug import pr_ref_from_url

if TYPE_CHECKING:
    from teatree.core.models.checkout_proof import ReleasedPrHeadAttestation

logger = logging.getLogger(__name__)
_MAX_REFLOG_ANCHORS = 64


class ReleasedPrHeadProof(NamedTuple):
    anchor_commit: str
    current_head: str
    pr_head: str
    remote_head: str
    pull_request: str


class OwnedGithubPrHeads(NamedTuple):
    url: str
    pr_head: str
    remote_head: str


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
        (current.holder, current.session) != claimant
        or (worktree.occupied_by, worktree.occupied_by_session) != claimant
        or any(entry.ended != RELEASED_EXIT for entry in previous)
    ):
        return False
    holder_ids: set[int] = set()
    for entry in previous:
        pk = task_id_from_holder(entry.holder)
        if pk is None:
            return False
        holder_ids.add(pk)
    prior_tasks = list(Task.objects.filter(pk__in=holder_ids, ticket=task.ticket))
    return bool(
        len(prior_tasks) == len(holder_ids)
        and all(prior.failure_kind != FailureKind.CANCELLED for prior in prior_tasks)
        and any(phase_writes_checkout(prior.phase) for prior in prior_tasks)
    )


def _owned_github_pr_heads(task: Task, path: str, state: CheckoutState, head: str) -> OwnedGithubPrHeads | None:
    origin = git.remote_slug(repo=path)
    origin_url = git.remote_url(repo=path)
    origin_host = host_from_remote(origin_url)
    if not origin or not origin_host or forge_from_remote(origin_url) != "github":
        return None
    pr_head = forge_pr_probe.github_open_pr_head(path, state.branch)
    if pr_head is None:
        return None
    ref = pr_ref_from_url(pr_head.url)
    if ref is None or ref.host_kind != "github":
        return None
    identity_matches = (
        ref.slug.casefold() == origin.casefold() and origin_host == (urlsplit(pr_head.url).hostname or "").lower()
    )
    head_matches = (
        pr_head.repository.casefold() == origin.casefold() and pr_head.branch == state.branch and pr_head.head == head
    )
    if not identity_matches or not head_matches:
        return None
    if not pr_owned_by_ticket(task, ref, pr_head.url):
        return None
    remote_head = forge_pr_probe.github_exact_branch_head(path, origin, state.branch, origin_host)
    return OwnedGithubPrHeads(pr_head.url, pr_head.head, remote_head) if remote_head == head else None


def _released_pr_head_proof(
    task: Task, worktree: Worktree, state: CheckoutState, custody: Custody
) -> ReleasedPrHeadProof | None:
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
        pr_heads = _owned_github_pr_heads(task, path, state, head)
        if pr_heads is None:
            return None
    except (OSError, RuntimeError, ValueError) as exc:
        logger.warning("Released PR head proof unreadable for checkout %s: %s", worktree.pk, exc)
        return None
    return ReleasedPrHeadProof(anchor, head, pr_heads.pr_head, pr_heads.remote_head, pr_heads.url)


def reattest_released_pr_head(task: Task, worktree: Worktree, state: CheckoutState, table: ProcessTable) -> bool:
    custody = Custody.from_extra(worktree.get_extra())
    if custody is None or len(custody.tenures) <= 1 or state.branch != worktree.branch:
        return False
    if (
        table.holds(Path(worktree.worktree_path))
        or table.eviction_refuse_reason(nested_writer=custody.prior_tenures_nested)
        or not _released_custody(task, worktree, custody)
    ):
        return False
    proof = _released_pr_head_proof(task, worktree, state, custody)
    if proof is None or not checkout_state_unchanged(task, worktree, state):
        return False
    current = custody.tenures[-1]
    audit: ReleasedPrHeadAttestation = {
        "at": timezone.now().isoformat(),
        "previous": custody.anchor,
        "digest": state.digest,
        "anchor_commit": proof.anchor_commit,
        "current_head": proof.current_head,
        "pr_head": proof.pr_head,
        "remote_head": proof.remote_head,
        "pull_request": proof.pull_request,
        "tenures": [entry.as_dict() for entry in custody.tenures],
        "process_table": checkout_process_evidence(table, custody),
        "holder": {"id": current.holder, "session": current.session},
    }
    attested = persist_checkout_recovery_attestation(worktree, custody, state.digest, audit)
    if attested:
        logger.info("Re-attested released PR-head checkout %s at %s", worktree.pk, proof.current_head)
    return attested
