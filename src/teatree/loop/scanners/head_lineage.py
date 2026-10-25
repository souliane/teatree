"""GitHub's answer to "did this PR's patch change between two of its heads?" (#5450, §17.4).

Feeds :func:`teatree.core.merge.head_carry_over.carry_over_head` the PR history and patch ids from GitHub.
Each added merge is replayed in a matching local clone: its tree must equal Git's automatic merge of its
parents. ``git patch-id --verbatim`` keeps indentation and trailing spaces in the identity. The patch id
covers context lines too, so main editing text next to a PR hunk refuses the carry.

Every uncertainty is explicit. A read that failed or a head that moved again returns ``None`` (retried, never
read as a verdict); a binary diff, a diff the forge will not render and a history it truncates return a refusal
naming the reason, because a patch id over partial data would carry a verdict onto code nobody graded.
"""

import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast

from teatree.config import clone_root
from teatree.core.merge.head_carry_over import SUPPORTED_HOST, LineageReader, PatchLineage, carry_over_head
from teatree.core.models import HeadCarryOver
from teatree.core.models.codex_review_marker import CodexReviewMarker
from teatree.core.worktree.clone_paths import unambiguous_clone_path
from teatree.loop.scanners.base import run_gh_for_slug
from teatree.project import find_project_root
from teatree.utils import git
from teatree.utils.forge import forge_from_remote
from teatree.utils.run import SUBPROCESS_UNREACHABLE, CommandFailedError, CompletedProcess, run_allowed_to_fail

logger = logging.getLogger(__name__)

GhRunner = Callable[[list[str], str], "CompletedProcess[str]"]

HISTORY_TRUNCATED = "history_truncated"
DIFF_UNAVAILABLE = "diff_unavailable"
HEAD_UNAVAILABLE = "head_unavailable"
_DIFF_ACCEPT = "Accept: application/vnd.github.diff"
_COMMITS_PER_PAGE = 250
_MERGE_PARENT_COUNT = 2
_UNRENDERABLE_STATUSES = ("HTTP 406", "HTTP 422")
_BINARY_DIFF = re.compile(r"^(?:Binary files .* differ|GIT binary patch)$", re.MULTILINE)


class _Ref(TypedDict, total=False):
    ref: str
    sha: str


class _Pull(TypedDict, total=False):
    base: _Ref
    head: _Ref


class _Commit(TypedDict, total=False):
    sha: str
    parents: list[object]


class _Compare(TypedDict, total=False):
    total_commits: int
    commits: list[_Commit]


class _UnreadableError(Exception):
    """A read failed or the head moved again: no verdict, retry."""


class _RefusedError(Exception):
    """The forge answered, but not completely enough to decide: refuse, naming why."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class GhLineageReader:
    """Reads a :class:`PatchLineage` through ``gh`` under the repo's routed token, never ambient authentication."""

    gh: GhRunner = run_gh_for_slug
    repo: Path | None = None

    def __call__(self, *, slug: str, pr_id: int, host_kind: str, old_head: str, new_head: str) -> PatchLineage | None:
        if host_kind != SUPPORTED_HOST:
            msg = f"GhLineageReader reads {SUPPORTED_HOST} pull requests only, not {host_kind!r}"
            raise ValueError(msg)
        try:
            return self._lineage(slug, pr_id, old_head, new_head)
        except _UnreadableError as unreadable:
            logger.warning("head lineage of %s#%d unreadable: %s", slug, pr_id, unreadable)
            return None
        except _RefusedError as refused:
            return PatchLineage("", "", descends=False, added_all_merges=False, refusal=refused.reason)

    def _lineage(self, slug: str, pr_id: int, old_head: str, new_head: str) -> PatchLineage:
        base = self._base_of(slug, pr_id, live_head=new_head)
        old_commits = self._commits(slug, base, old_head)
        new_commits = self._commits(slug, base, new_head)
        added = new_commits.keys() - old_commits.keys()
        descends = old_head in new_commits
        added_all_merges = all(len(new_commits[sha]) == _MERGE_PARENT_COUNT for sha in added)
        if not descends or not added_all_merges:
            return PatchLineage("", "", descends=descends, added_all_merges=added_all_merges)
        old_patch_id = self._patch_id(slug, base, old_head)
        new_patch_id = self._patch_id(slug, base, new_head)
        return PatchLineage(
            old_patch_id=old_patch_id,
            new_patch_id=new_patch_id,
            descends=descends,
            added_all_merges=added_all_merges,
            refusal=self._auto_merge_refusal(slug, new_head, {sha: new_commits[sha] for sha in added}),
        )

    def _base_of(self, slug: str, pr_id: int, *, live_head: str) -> str:
        pull = cast("_Pull", self._object(slug, f"repos/{slug}/pulls/{pr_id}"))
        base = pull.get("base", {}).get("ref", "")
        if not base or pull.get("head", {}).get("sha") != live_head:
            msg = f"{slug}#{pr_id} no longer points at {live_head[:8]}"
            raise _UnreadableError(msg)
        return base

    def _commits(self, slug: str, base: str, head: str) -> dict[str, tuple[str, ...]]:
        compare = cast(
            "_Compare", self._object(slug, f"repos/{slug}/compare/{base}...{head}?per_page={_COMMITS_PER_PAGE}")
        )
        commits = compare.get("commits", [])
        if compare.get("total_commits", 0) > len(commits):
            raise _RefusedError(HISTORY_TRUNCATED)
        return {
            commit["sha"]: tuple(
                str(parent.get("sha", "")) for parent in commit.get("parents", []) if isinstance(parent, dict)
            )
            for commit in commits
        }

    def _repo_for(self, slug: str) -> Path | None:
        if self.repo is not None:
            return self.repo
        candidates = (find_project_root(), unambiguous_clone_path(clone_root(), slug))
        for repo in candidates:
            if repo is not None:
                try:
                    if git.remote_slug(repo=str(repo)).casefold() == slug.casefold():
                        return repo
                except (CommandFailedError, *SUBPROCESS_UNREACHABLE):
                    continue
        return None

    def _auto_merge_refusal(self, slug: str, new_head: str, added: dict[str, tuple[str, ...]]) -> str:
        repo = self._repo_for(slug)
        if repo is None:
            return "merge_tree_unavailable"
        try:
            present = run_allowed_to_fail(
                ["git", "-C", str(repo), "cat-file", "-e", f"{new_head}^{{commit}}"], expected_codes=None, timeout=5
            )
            if present.returncode != 0:
                fetched = run_allowed_to_fail(
                    ["git", "-C", str(repo), "fetch", "--no-tags", "origin", new_head],
                    expected_codes=None,
                    timeout=30,
                )
                if fetched.returncode != 0:
                    return "merge_tree_unavailable"
            for sha, pair in added.items():
                if len(pair) != _MERGE_PARENT_COUNT or not all(pair):
                    return "merge_tree_unavailable"
                merged = run_allowed_to_fail(
                    ["git", "-C", str(repo), "merge-tree", "--write-tree", *pair], expected_codes=None, timeout=20
                )
                actual = run_allowed_to_fail(
                    ["git", "-C", str(repo), "show", "-s", "--format=%T", sha], expected_codes=None, timeout=5
                )
                if (
                    merged.returncode != 0
                    or actual.returncode != 0
                    or merged.stdout.partition("\n")[0] != actual.stdout.strip()
                ):
                    return "merge_tree_differs"
        except SUBPROCESS_UNREACHABLE:
            return "merge_tree_unavailable"
        return ""

    def _patch_id(self, slug: str, base: str, head: str) -> str:
        diff = self._stdout(slug, ["api", "-H", _DIFF_ACCEPT, f"repos/{slug}/compare/{base}...{head}"], diff=True)
        if _BINARY_DIFF.search(diff):
            raise _RefusedError(DIFF_UNAVAILABLE)
        result = run_allowed_to_fail(["git", "patch-id", "--verbatim"], expected_codes=None, stdin_text=diff)
        if result.returncode != 0:
            msg = f"git patch-id exited {result.returncode}: {result.stderr.strip()[:200]}"
            raise _UnreadableError(msg)
        return result.stdout.split(maxsplit=1)[0] if result.stdout.strip() else ""

    def _object(self, slug: str, endpoint: str) -> Mapping[str, object]:
        try:
            payload = json.loads(self._stdout(slug, ["api", endpoint]))
        except json.JSONDecodeError as exc:
            msg = f"{endpoint} returned non-JSON"
            raise _UnreadableError(msg) from exc
        if not isinstance(payload, dict):
            msg = f"{endpoint} returned {type(payload).__name__}, not an object"
            raise _UnreadableError(msg)
        return payload

    def _stdout(self, slug: str, argv: list[str], *, diff: bool = False) -> str:
        result = self.gh(argv, slug)
        if result.returncode == 0:
            return result.stdout
        if diff and any(status in result.stderr for status in _UNRENDERABLE_STATUSES):
            raise _RefusedError(DIFF_UNAVAILABLE)
        if "/compare/" in argv[-1] and "HTTP 404" in result.stderr:
            raise _RefusedError(HEAD_UNAVAILABLE)
        msg = f"gh {argv[-1]} exited {result.returncode}: {result.stderr.strip()[:200]}"
        raise _UnreadableError(msg)


def carried_head_row(
    *, slug: str, pr_id: int, head_sha: str, host_kind: str, reader: LineageReader
) -> HeadCarryOver | None:
    """The ledger row deciding *head_sha*, or ``None`` when none is recorded yet.

    A failure of the carry itself never blocks the caller — it proceeds exactly as it did before the carry existed.
    """
    try:
        return carry_over_head(slug=slug, pr_id=pr_id, live_head=head_sha, host_kind=host_kind, read_lineage=reader)
    except Exception:
        logger.exception("head carry-over failed for %s#%d at %s", slug, pr_id, head_sha[:8])
        return None


def head_is_carried(*, slug: str, pr_id: int, head_sha: str, host_kind: str, reader: LineageReader) -> bool:
    """Whether *head_sha* arrived from an earlier head with the patch unchanged, so its records may have followed."""
    row = carried_head_row(slug=slug, pr_id=pr_id, head_sha=head_sha, host_kind=host_kind, reader=reader)
    return row is not None and row.outcome == HeadCarryOver.Outcome.CARRIED


def review_head_is_carried(*, slug: str, pr_id: int, head_sha: str, pr_url: str, variant: str) -> bool:
    """Whether a review verdict followed an unchanged patch to *head_sha*: arming another round on it is churn.

    The review-arming handlers read the live forge under their routed token. A head the patch left unchanged
    that nothing reviewed (a branch update on a PR with no verdict) is still reviewed in the normal round.
    """
    row = carried_head_row(
        slug=slug, pr_id=pr_id, head_sha=head_sha, host_kind=forge_from_remote(pr_url), reader=GhLineageReader()
    )
    if row is None or not row.reviewed_by_carry:
        return False
    ancestors: set[str] = set()
    prior = row.from_head
    while prior not in ancestors:
        ancestors.add(prior)
        previous = HeadCarryOver.objects.decided(slug=slug, pr_id=pr_id, head=prior)
        if previous is None or not previous.reviewed_by_carry:
            break
        prior = previous.from_head
    return CodexReviewMarker.objects.filter(
        slug__iexact=slug.strip(),
        pr_id=pr_id,
        head_sha__in=ancestors,
        variant=variant,
        state=CodexReviewMarker.State.RESOLVED,
    ).exists()
