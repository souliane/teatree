"""Pairwise overlap of one repo's open PR heads: which fold, which wait, which merge first.

The merge keystone and the red-PR fix dispatch both ask :func:`overlap_refusal` before they
act, so two PRs that edit the same lines are serialised (older PR number first) instead of
both being worked or merged and the second one conflicting.
"""

import itertools
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from teatree.core.backend_protocols import BackendResolutionError
from teatree.core.merge.carrier import carried_pr_ids, frozen_by_carrier
from teatree.core.merge.ci_rollup import CodeHostQuery
from teatree.core.models import PullRequest, Ticket
from teatree.core.worktree.branch_currency import predict_merge_conflicts
from teatree.core.worktree.clone_paths import matching_clone
from teatree.types import RawAPIDict
from teatree.utils.git import git_env_without_overrides
from teatree.utils.pr_ref import PrRef
from teatree.utils.run import TimeoutExpired, run_allowed_to_fail

_FORGE_READ_FAILURES = (BackendResolutionError, LookupError, OSError, RuntimeError, ValueError, TimeoutExpired)
_OPEN_STATE = "open"


class OpenHeadsUnreadableError(RuntimeError):
    """The forge did not name every open head; an empty answer here would read as 'nothing overlaps'."""


class OverlapVerdict(StrEnum):
    FOLD = "fold"
    FREEZE = "freeze"
    ORDER = "order"
    SHARED = "shared"


class OpenPrLister(Protocol):
    def list_prs(self, *, repo: str, state: str = "", author: str = "") -> list[RawAPIDict]: ...

    def fetch_live_head_sha(self, *, slug: str, pr_id: int) -> str: ...


@dataclass(frozen=True, slots=True)
class OpenHead:
    pr_id: int
    sha: str
    ticket_id: int | None = None
    carries: frozenset[int] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class PairOverlap:
    older: OpenHead
    newer: OpenHead
    verdict: OverlapVerdict
    conflict_paths: tuple[str, ...]
    shared_paths: tuple[str, ...]


def _head_sha(raw: Mapping[str, object], *, host: OpenPrLister, repo: str, pr_id: int) -> str:
    head = raw.get("head")
    listed = head.get("sha") if isinstance(head, Mapping) else raw.get("sha") or raw.get("headRefOid")
    sha = str(listed or "")
    if sha:
        return sha
    try:
        return host.fetch_live_head_sha(slug=repo, pr_id=pr_id).strip()
    except _FORGE_READ_FAILURES:
        return ""


def read_open_heads(repo: str, *, host: OpenPrLister) -> tuple[OpenHead, ...]:
    """Every open head of *repo* from ONE forge list call; raises :class:`OpenHeadsUnreadableError` when unreadable."""
    try:
        listed = host.list_prs(repo=repo, state=_OPEN_STATE)
    except _FORGE_READ_FAILURES as exc:
        msg = f"the open PRs of {repo} could not be listed: {exc}"
        raise OpenHeadsUnreadableError(msg) from exc
    pr_ids = [pr_id for raw in listed if isinstance(pr_id := raw.get("number") or raw.get("iid"), int)]
    if len(pr_ids) != len(listed):
        msg = f"the open PR list of {repo} carries an entry with no PR number"
        raise OpenHeadsUnreadableError(msg)
    ticket_by_iid = dict(
        PullRequest.objects.filter(repo__iexact=repo, iid__in=[str(pr_id) for pr_id in pr_ids]).values_list(
            "iid", "ticket_id"
        )
    )
    heads = []
    for raw, pr_id in zip(listed, pr_ids, strict=True):
        if not (sha := _head_sha(raw, host=host, repo=repo, pr_id=pr_id)):
            msg = f"the head of {repo}#{pr_id} could not be read"
            raise OpenHeadsUnreadableError(msg)
        body = str(raw.get("body") or raw.get("description") or "")
        heads.append(
            OpenHead(
                pr_id=pr_id,
                sha=sha,
                ticket_id=ticket_by_iid.get(str(pr_id)),
                carries=carried_pr_ids(body, repo=repo),
            )
        )
    return tuple(heads)


def _git(repo_dir: str, *args: str) -> tuple[int, list[str]]:
    result = run_allowed_to_fail(["git", "-C", repo_dir, *args], expected_codes=None, env=git_env_without_overrides())
    return result.returncode, result.stdout.split()


def _changed_paths(repo_dir: str, base: str, sha: str) -> frozenset[str]:
    code, paths = _git(repo_dir, "diff", "--name-only", base, sha)
    return frozenset(paths) if code == 0 else frozenset()


def _shared_paths(repo_dir: str, older_sha: str, newer_sha: str) -> tuple[str, ...]:
    code, bases = _git(repo_dir, "merge-base", older_sha, newer_sha)
    if code != 0 or not bases:
        return ()
    shared = _changed_paths(repo_dir, bases[0], older_sha) & _changed_paths(repo_dir, bases[0], newer_sha)
    return tuple(sorted(shared))


def _verdict(
    older: OpenHead,
    newer: OpenHead,
    *,
    carried: frozenset[int],
    conflict_paths: tuple[str, ...],
    shared_paths: tuple[str, ...],
) -> OverlapVerdict | None:
    if older.pr_id in newer.carries or newer.pr_id in older.carries:
        return OverlapVerdict.FOLD
    if not (conflict_paths or shared_paths):
        return None
    if older.ticket_id is not None and older.ticket_id == newer.ticket_id:
        return OverlapVerdict.FOLD
    if not conflict_paths:
        return OverlapVerdict.SHARED
    if {older.pr_id, newer.pr_id} & carried:
        return OverlapVerdict.FREEZE
    return OverlapVerdict.ORDER


def pair_overlaps(heads: tuple[OpenHead, ...], *, repo_dir: str) -> tuple[PairOverlap, ...]:
    """Every overlapping pair among *heads*, older PR number first; pairs that touch nothing in common are omitted."""
    carried = frozenset().union(*(head.carries for head in heads))
    found = []
    for older, newer in itertools.combinations(sorted(heads, key=lambda head: head.pr_id), 2):
        conflict_paths = predict_merge_conflicts(repo_dir, newer.sha, older.sha) or ()
        shared_paths = _shared_paths(repo_dir, older.sha, newer.sha)
        verdict = _verdict(older, newer, carried=carried, conflict_paths=conflict_paths, shared_paths=shared_paths)
        if verdict is not None:
            found.append(PairOverlap(older, newer, verdict, tuple(conflict_paths), shared_paths))
    return tuple(found)


def overlap_refusal(
    *,
    ref: PrRef,
    ticket: Ticket | None,
    host: OpenPrLister | None = None,
    repo_dir: str | None = None,
) -> str | None:
    """Why *ref* must wait, else ``None``: a carrier-frozen member, an unreadable list, or an older conflict."""
    if ticket is not None and (carrier_url := frozen_by_carrier(ticket)) is not None:
        return f"{ref.slug}#{ref.pr_id} belongs to the merge round of carrier {carrier_url}, which is still open"
    try:
        heads = read_open_heads(ref.slug, host=host or CodeHostQuery.for_ref(ref).backend)
    except OpenHeadsUnreadableError as exc:
        return (
            f"the open PR heads of {ref.slug} could not be read ({exc}); an unreadable list cannot rule out an overlap"
        )
    clone = repo_dir or (str(path) if (path := matching_clone(ref.slug)) else "")
    if not clone:
        return None
    for pair in pair_overlaps(heads, repo_dir=clone):
        if pair.verdict is OverlapVerdict.ORDER and pair.newer.pr_id == ref.pr_id:
            paths = ", ".join(pair.conflict_paths)
            return (
                f"{ref.slug}#{ref.pr_id} conflicts with the older open {ref.slug}#{pair.older.pr_id} on {paths}; "
                f"merge #{pair.older.pr_id} first"
            )
    return None
