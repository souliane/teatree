"""Re-attest a stranded checkout when its custody and host state prove it safe."""

import logging
from pathlib import Path
from urllib.parse import urlsplit

from django.utils import timezone

from teatree.core import forge_pr_probe
from teatree.core.cleanup.process_table import ProcessTable
from teatree.core.modelkit.phase_tools import MCP_WRITE, tools_for_phase
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
from teatree.core.models.worktree_occupancy import task_holder_id
from teatree.core.worktree.checkout_state import CheckoutState, checkout_digest, checkout_state
from teatree.utils import git
from teatree.utils.forge import forge_from_remote
from teatree.utils.git_remote import host_from_remote
from teatree.utils.run import CommandFailedError
from teatree.utils.url_slug import pr_ref_from_url

logger = logging.getLogger(__name__)
_MAX_REFLOG_ANCHORS = 64


def _holder_task(holder: str, task: Task) -> Task | None:
    pk = holder.removeprefix("task:")
    if not holder.startswith("task:") or not pk.isdigit():
        return None
    return Task.objects.filter(pk=int(pk), ticket=task.ticket).first()


def _cancelled(task: Task | None) -> bool:
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
    prior = _holder_task(prior_holder, task)
    if prior is None or not tools_for_phase(prior.phase) & {"write_file", "edit_file", MCP_WRITE} or _cancelled(prior):
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


def _process_evidence(table: ProcessTable, custody: Custody) -> CheckoutProcessEvidence:
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
    if prior is None or branch_proof is None or not _state_unchanged(task, worktree, state):
        return False
    branches, pull_requests = branch_proof
    current: Tenure = custody.tenures[-1]
    audit: AutoAttestation = {
        "at": timezone.now().isoformat(),
        "previous": custody.anchor,
        "digest": state.digest,
        "tenures": [entry.as_dict() for entry in custody.tenures],
        "process_table": _process_evidence(table, custody),
        "branches": branches,
        "pull_requests": pull_requests,
        "holder": {"id": current.holder, "session": current.session},
    }
    if not _attest(worktree, custody, state.digest, audit):
        return False
    logger.info("Re-attested checkout %s after task %s", worktree.pk, prior.pk)
    return True


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
    prior_tasks = [_holder_task(entry.holder, task) for entry in previous]
    return bool(
        not current.ended
        and (current.holder, current.session) == claimant
        and (worktree.occupied_by, worktree.occupied_by_session) == claimant
        and all(entry.ended == "released" for entry in previous)
        and all(prior is not None and prior.failure_kind != FailureKind.CANCELLED for prior in prior_tasks)
        and any(
            prior is not None and tools_for_phase(prior.phase) & {"write_file", "edit_file", MCP_WRITE}
            for prior in prior_tasks
        )
    )


def _owned_github_head_url(task: Task, path: str, state: CheckoutState, head: str) -> str | None:
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
    return pr_url if forge_pr_probe.github_exact_branch_head(path, origin, state.branch, origin_host) == head else None


def _released_pr_head_proof(
    task: Task, worktree: Worktree, state: CheckoutState, custody: Custody
) -> tuple[str, str, str] | None:
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
        pr_url = _owned_github_head_url(task, path, state, head)
        if pr_url is None:
            return None
    except (CommandFailedError, OSError, RuntimeError, ValueError) as exc:
        logger.warning("Released PR head proof unreadable for checkout %s: %s", worktree.pk, exc)
        return None
    return anchor, head, pr_url


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
    anchor, head, pr_url = proof
    current = custody.tenures[-1]
    audit: ReleasedPrHeadAttestation = {
        "at": timezone.now().isoformat(),
        "previous": custody.anchor,
        "digest": state.digest,
        "anchor_commit": anchor,
        "current_head": head,
        "remote_head": head,
        "pull_request": pr_url,
        "tenures": [entry.as_dict() for entry in custody.tenures],
        "process_table": _process_evidence(table, custody),
        "holder": {"id": current.holder, "session": current.session},
    }
    attested = _attest(worktree, custody, state.digest, audit)
    if attested:
        logger.info("Re-attested released PR-head checkout %s at %s", worktree.pk, head)
    return attested


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
    if _cancelled(_holder_task(record["holder"], task)) or not _state_unchanged(task, worktree, state):
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
