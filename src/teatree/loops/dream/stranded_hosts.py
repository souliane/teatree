from dataclasses import dataclass, field
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


@dataclass(slots=True)
class _RemoteListings:
    by_repo: dict[str, RemoteRefListing] = field(default_factory=dict)
    stopped: bool = False

    def of(self, repo: str) -> RemoteRefListing:
        if repo not in self.by_repo:
            listing = RemoteRefListing(None) if self.stopped else list_remote_refs(repo)
            self.stopped = self.stopped or listing.timed_out
            self.by_repo[repo] = listing
        return self.by_repo[repo]


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


def _branch_release_reason(host: Ticket, listings: _RemoteListings) -> str:
    branch = _branch_name(host)
    if branch is None:
        return ""
    if not branch:
        return "idle with no recorded branch"
    clone = _clone_for(host)
    if clone is None:
        return ""
    repo = str(clone)
    presence = ref_presence(repo, branch, listings.of(repo))
    if presence is RefPresence.UNKNOWN:
        return ""
    if presence is RefPresence.ABSENT:
        return f"branch {branch} absent locally, in tracking, and on origin"
    target = effective_default_target(repo)
    verdict = branch_redundancy(repo, branch, target)
    return f"branch {branch} redundant on {target}" if verdict.redundant else ""


def _hold_reason(host: Ticket, *, shell: bool) -> str:
    if host.state in {Ticket.State.PR_OPENED, Ticket.State.REVIEW_REQUESTED}:
        return "pull request state"
    if PullRequest.objects.filter(ticket=host).exists():
        return "pull request row"
    if host.sessions.live().exists():
        return "live session"
    if any(not row.is_stale for row in host.worktrees.all()):
        return "live worktree"
    if shell and (host.extra or {}).get("dream_gap_dispositions"):
        return "recorded dispositions"
    return ""


def _stranded(now: datetime | None) -> list[tuple[Ticket, datetime]]:
    cutoff = (now or timezone.now()) - timedelta(days=STRANDED_IDLE_DAYS)
    hosts = Ticket.objects.exclude(extra__dream_gap_batch__isnull=True).exclude(state__in=Ticket.finished_states())
    found = [
        (host, activity)
        for host in hosts
        if batch_entries(host)
        and (activity := host.last_activity_at()) is not None
        and activity <= cutoff
        and not host.tasks.filter(status__in=Task.Status.active()).exists()
    ]
    found.sort(key=lambda item: (-len(batch_entries(item[0])), item[1], item[0].pk))
    return found


def stranded_host_count(now: datetime | None = None) -> int:
    return len(_stranded(now))


def _return_gaps(host: Ticket, keys: list[str], reason: str, *, shell: bool) -> list[str]:
    with transaction.atomic():
        if shell:
            return return_member_gaps(host, advanced=True, reason=reason)
        for key in keys:
            return_gap(host, key, reason)
        return keys


def release_stranded_hosts(
    *, umbrella_url: str, now: datetime | None = None, dry_run: bool = False
) -> StrandedReleaseReport:
    released = gaps = held = 0
    listings = _RemoteListings()
    for host, _activity in _stranded(now):
        if released == RELEASE_HOSTS_PER_PASS:
            break
        shell = _is_legacy_shell(host, umbrella_url)
        reason = "" if _hold_reason(host, shell=shell) else _branch_release_reason(host, listings)
        if not reason:
            held += 1
            continue
        keys = undispositioned_gap_keys(host)
        if not keys:
            continue
        try:
            returned = keys if dry_run else _return_gaps(host, keys, reason, shell=shell)
        except DreamGapLedgerError:
            held += 1
            continue
        released += 1
        gaps += len(returned)
    return StrandedReleaseReport(released_hosts=released, returned_gaps=gaps, held_hosts=held)
