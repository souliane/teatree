from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from teatree.config import clone_root
from teatree.core.models import PullRequest, Task, Ticket
from teatree.core.models.dream_gap_ledger import (
    DreamGapLedgerError,
    batch_entries,
    return_gap,
    return_member_gaps,
    undispositioned_gap_keys,
)
from teatree.core.worktree.branch_classification import branch_redundancy, effective_default_target
from teatree.core.worktree.clone_paths import stored_clone_path, unambiguous_clone_path
from teatree.core.worktree.dev_repo import issue_url_slug
from teatree.utils.git_branch import RefPresence, RemoteRefListing, list_remote_refs, ref_presence

STRANDED_IDLE_DAYS = 14
RELEASE_HOSTS_PER_PASS = 25


@dataclass(frozen=True, slots=True)
class StrandedReleaseReport:
    released_hosts: int = 0
    returned_gaps: int = 0
    held_hosts: int = 0


def _is_legacy_shell(host: Ticket, umbrella_url: str) -> bool:
    base, separator, fragment = host.issue_url.partition("#")
    return base == umbrella_url and bool(separator) and fragment.startswith(("dream-gap=", "dream-batch="))


def _clone_for(host: Ticket) -> Path | None:
    stored = {path for row in host.worktrees.all() if (path := stored_clone_path(row)) is not None}
    if len(stored) > 1:
        return None
    if stored:
        return next(iter(stored))
    names = {name for name in (host.repos or []) if isinstance(name, str) and name}
    if not names:
        names = {issue_url_slug(host.issue_url)} - {None, ""}
    if len(names) != 1:
        return None
    return unambiguous_clone_path(clone_root(), next(iter(names)))


def _branch_name(host: Ticket) -> str | None:
    recorded = (host.extra or {}).get("branch")
    names = [recorded, *(row.branch for row in host.worktrees.all())]
    branches = {name for name in names if isinstance(name, str) and name}
    if len(branches) > 1:
        return None
    return next(iter(branches), "")


def _branch_release_reason(host: Ticket, listing: RemoteRefListing | None) -> tuple[str, RemoteRefListing | None]:
    branch = _branch_name(host)
    if branch is None:
        return "", listing
    if not branch:
        return "idle with no recorded branch", listing
    clone = _clone_for(host)
    if clone is None:
        return "", listing
    repo = str(clone)
    if listing is None:
        listing = list_remote_refs(repo)
    presence = ref_presence(repo, branch, listing=listing) if listing.repo == repo else RefPresence.UNKNOWN
    if presence is RefPresence.UNKNOWN:
        return "", listing
    if presence is RefPresence.ABSENT:
        return f"branch {branch} absent locally, in tracking, and on origin", listing
    target = effective_default_target(repo)
    verdict = branch_redundancy(repo, branch, target)
    return (f"branch {branch} redundant on {target}" if verdict.redundant else ""), listing


def _hold_reason(host: Ticket, *, shell: bool) -> str:
    if host.state in {Ticket.State.PR_OPENED, Ticket.State.REVIEW_REQUESTED}:
        return "pull request state"
    if PullRequest.objects.filter(ticket=host).exists():
        return "pull request row"
    if host.tasks.filter(status__in=Task.Status.active()).exists() or host.sessions.live().exists():
        return "live work"
    if any(not row.is_stale for row in host.worktrees.all()):
        return "live worktree"
    if shell and (host.extra or {}).get("dream_gap_dispositions"):
        return "recorded dispositions"
    return ""


def release_stranded_hosts(
    *, umbrella_url: str, now: datetime | None = None, dry_run: bool = False
) -> StrandedReleaseReport:
    cutoff = (now or timezone.now()) - timedelta(days=STRANDED_IDLE_DAYS)
    hosts = Ticket.objects.exclude(extra__dream_gap_batch__isnull=True).exclude(state__in=Ticket.finished_states())
    candidates = [
        (host, activity)
        for host in hosts
        if batch_entries(host) and (activity := host.last_activity_at()) is not None and activity <= cutoff
    ]
    candidates.sort(key=lambda item: (-len(batch_entries(item[0])), item[1], item[0].pk))
    released = gaps = held = 0
    listing: RemoteRefListing | None = None
    for host, _activity in candidates[:RELEASE_HOSTS_PER_PASS]:
        shell = _is_legacy_shell(host, umbrella_url)
        if _hold_reason(host, shell=shell):
            held += 1
            continue
        reason, listing = _branch_release_reason(host, listing)
        if not reason:
            held += 1
            continue
        keys = undispositioned_gap_keys(host)
        if not keys:
            continue
        if dry_run:
            continue
        try:
            with transaction.atomic():
                if shell:
                    returned = return_member_gaps(host, advanced=True, reason=reason)
                else:
                    for key in keys:
                        return_gap(host, key, reason)
                    returned = keys
        except DreamGapLedgerError:
            held += 1
            continue
        released += 1
        gaps += len(returned)
    return StrandedReleaseReport(released_hosts=released, returned_gaps=gaps, held_hosts=held)
