"""GitHub's answer to "did this PR's patch change between two of its heads?" (#5450, §17.4).

Feeds :func:`teatree.core.merge.head_carry_over.carry_over_head` the facts it decides on, read from the forge
alone: the commits each head adds to the PR beyond its base, with their parent counts, and the patch id of the
PR's diff against its base at each head (``git patch-id --verbatim``: nothing stripped, because indentation is
code). The patch id covers the diff's context lines too, so main editing text next to a PR hunk changes it and
the carry is refused — only the delta is then graded by the normal review round.

Every uncertainty is explicit. A read that failed or a head that moved again returns ``None`` (retried, never
read as a verdict); a binary diff, a diff the forge will not render and a history it truncates return a refusal
naming the reason, because a patch id over partial data would carry a verdict onto code nobody graded.
"""

import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TypedDict, cast

from teatree import forge_credentials
from teatree.core.merge.head_carry_over import SUPPORTED_HOST, LineageReader, PatchLineage, carry_over_head
from teatree.core.models import HeadCarryOver
from teatree.loop.scanners.base import run_gh
from teatree.utils.forge import forge_from_remote
from teatree.utils.run import CompletedProcess, run_allowed_to_fail

logger = logging.getLogger(__name__)

GhRunner = Callable[[list[str], str], "CompletedProcess[str]"]

HISTORY_TRUNCATED = "history_truncated"
DIFF_UNAVAILABLE = "diff_unavailable"
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


def credentialed_gh(argv: list[str], slug: str) -> "CompletedProcess[str]":
    resolution = forge_credentials.resolve_slug_token(slug, forge="github", credential="github_token")
    return run_gh(argv, resolution=resolution)


@dataclass(frozen=True, slots=True)
class GhLineageReader:
    """Reads a :class:`PatchLineage` through ``gh`` under the repo's routed token, never ambient authentication."""

    gh: GhRunner = credentialed_gh

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
        return PatchLineage(
            old_patch_id=self._patch_id(slug, base, old_head),
            new_patch_id=self._patch_id(slug, base, new_head),
            descends=old_head in new_commits,
            added_all_merges=all(new_commits[sha] >= _MERGE_PARENT_COUNT for sha in added),
        )

    def _base_of(self, slug: str, pr_id: int, *, live_head: str) -> str:
        pull = cast("_Pull", self._object(slug, f"repos/{slug}/pulls/{pr_id}"))
        base = pull.get("base", {}).get("ref", "")
        if not base or pull.get("head", {}).get("sha") != live_head:
            msg = f"{slug}#{pr_id} no longer points at {live_head[:8]}"
            raise _UnreadableError(msg)
        return base

    def _commits(self, slug: str, base: str, head: str) -> dict[str, int]:
        """The PR-only commits *head* adds beyond *base*, mapped to their parent counts."""
        compare = cast(
            "_Compare", self._object(slug, f"repos/{slug}/compare/{base}...{head}?per_page={_COMMITS_PER_PAGE}")
        )
        commits = compare.get("commits", [])
        if compare.get("total_commits", 0) > len(commits):
            raise _RefusedError(HISTORY_TRUNCATED)
        return {commit["sha"]: len(commit.get("parents", [])) for commit in commits}

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
        msg = f"gh {argv[-1]} exited {result.returncode}: {result.stderr.strip()[:200]}"
        raise _UnreadableError(msg)


def head_is_carried(*, slug: str, pr_id: int, head_sha: str, host_kind: str, reader: LineageReader) -> bool:
    """Whether *head_sha*'s records followed an unchanged patch from an earlier head.

    The question both the sweep and the review-arming handlers ask: a carried head was already reviewed, so
    arming another round on it is churn. A failure of the carry itself never blocks the caller — it proceeds
    exactly as it did before the carry existed.
    """
    try:
        row = carry_over_head(slug=slug, pr_id=pr_id, live_head=head_sha, host_kind=host_kind, read_lineage=reader)
    except Exception:
        logger.exception("head carry-over failed for %s#%d at %s", slug, pr_id, head_sha[:8])
        return False
    return row is not None and row.outcome == HeadCarryOver.Outcome.CARRIED


def review_head_is_carried(*, slug: str, pr_id: int, head_sha: str, pr_url: str) -> bool:
    """:func:`head_is_carried` for a review-arming handler, which reads the live forge under its routed token."""
    return head_is_carried(
        slug=slug, pr_id=pr_id, head_sha=head_sha, host_kind=forge_from_remote(pr_url), reader=GhLineageReader()
    )
