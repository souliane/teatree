"""Production I/O adapters for :class:`teatree.loop.scanners.pr_sweep.PrSweepScanner`.

The scanner core (decision ladder, signals) lives in ``pr_sweep``; this module
holds the side-effecting implementations of its three injected ports — the
``gh``-backed :class:`PrApiClient`, the ``call_command`` :class:`MergeKeystone`,
and the Slack :class:`MergeNotifier` (plus a null notifier) — together with the
``gh pr list --json`` decoding. Splitting the adapters out keeps the scanner
module focused on logic and under the module-health LOC cap.
"""

import json
import os
import shutil
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypedDict, cast

from teatree.loop.scanners.base import ScannerError, classify_gh_stderr
from teatree.loop.scanners.pr_sweep import GH_CONFLICT_MERGE_STATE, GH_CONFLICT_MERGEABLE, PrSummary
from teatree.loop.scanners.pr_sweep_types import MERGEABLE_AWAITING_REVIEW_REASON as _MERGEABLE_AWAITING_REVIEW_REASON
from teatree.utils.run import run_allowed_to_fail

if TYPE_CHECKING:
    from teatree.types import RawAPIDict

_GH_NOT_INSTALLED_RC = 127


class GhPrJson(TypedDict, total=False):
    """Shape of one ``gh pr list --json …`` entry the scanner consumes."""

    number: int
    headRefOid: str
    isDraft: bool
    url: str
    title: str
    reviews: list[object]
    statusCheckRollup: list[object]
    mergeable: str
    mergeStateStatus: str
    author: "GhAuthorJson"


class GhAuthorJson(TypedDict, total=False):
    """Shape of the ``GhPrJson.author`` block — the PR author identity."""

    login: str


class GhReviewJson(TypedDict, total=False):
    """Shape of one review entry inside ``GhPrJson.reviews``."""

    state: str


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _author_login(raw: GhPrJson) -> str:
    """Read the PR author's login from the ``gh pr list --json author`` block."""
    author = raw.get("author")
    if isinstance(author, dict):
        return _as_str(author.get("login"))
    return ""


def _decode_pr(*, slug: str, raw: GhPrJson) -> PrSummary:
    number_raw = raw.get("number")
    number = number_raw if isinstance(number_raw, int) else 0
    head_sha = _as_str(raw.get("headRefOid"))
    is_draft = bool(raw.get("isDraft"))
    url = _as_str(raw.get("url"))
    title = _as_str(raw.get("title"))
    reviews_raw = raw.get("reviews")
    reviews: list[object] = list(reviews_raw) if isinstance(reviews_raw, list) else []
    rollup_raw = raw.get("statusCheckRollup")
    rollup: list[object] = list(rollup_raw) if isinstance(rollup_raw, list) else []
    return PrSummary(
        slug=slug,
        number=number,
        head_sha=head_sha,
        is_draft=is_draft,
        has_changes_requested=_has_changes_requested(reviews),
        rollup=tuple(cast("RawAPIDict", item) for item in rollup if isinstance(item, dict)),
        url=url,
        title=title,
        is_conflicted=_gh_is_conflicted(raw),
        behind_main=_gh_is_behind_main(raw),
        author=_author_login(raw),
    )


def _gh_is_behind_main(raw: GhPrJson) -> bool:
    """True iff GitHub reports the branch as behind its base (#2045).

    ``mergeStateStatus == "BEHIND"`` is a clean branch whose base advanced —
    distinct from ``DIRTY`` (a hard conflict). A repo-state check red on a
    behind branch is the rerun-can't-fix case the sweep surfaces as
    ``needs_branch_update``.
    """
    return _as_str(raw.get("mergeStateStatus")).upper() == "BEHIND"


def _gh_is_conflicted(raw: GhPrJson) -> bool:
    """True iff GitHub reports the PR as a hard merge conflict (#78).

    Reads the two conflict signals ``gh pr list --json`` exposes:
    ``mergeable == "CONFLICTING"`` and ``mergeStateStatus == "DIRTY"``.
    ``UNKNOWN`` / ``BEHIND`` / ``MERGEABLE`` / empty are never conflicts —
    a behind-but-clean branch is not flagged, and a still-computing
    mergeability state is left for a later tick rather than raising a
    false alarm.
    """
    mergeable = _as_str(raw.get("mergeable")).upper()
    merge_state = _as_str(raw.get("mergeStateStatus")).upper()
    return mergeable == GH_CONFLICT_MERGEABLE or merge_state == GH_CONFLICT_MERGE_STATE


def _has_changes_requested(reviews: list[object]) -> bool:
    """True iff any review on the PR is in ``CHANGES_REQUESTED`` state."""
    for review in reviews:
        if not isinstance(review, dict):
            continue
        review_dict = cast("GhReviewJson", review)
        state = _as_str(review_dict.get("state")).upper()
        if state == "CHANGES_REQUESTED":
            return True
    return False


@dataclass(slots=True)
class GhPrApiClient:
    """``gh``-backed :class:`teatree.loop.scanners.pr_sweep.PrApiClient`.

    *token* — when non-empty — is exported as ``GH_TOKEN`` for every
    subprocess call so the scanner can hit a private repo on behalf of a
    given overlay using that overlay's PAT.
    """

    token: str = ""

    def list_open_prs(self, *, slug: str) -> list[PrSummary]:
        argv = [
            "pr",
            "list",
            "--repo",
            slug,
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "number,headRefOid,isDraft,url,title,reviews,statusCheckRollup,mergeable,mergeStateStatus,author",
        ]
        rc, out, err = self._run_gh(argv)
        if rc == _GH_NOT_INSTALLED_RC:
            # gh-not-installed is an environmental error, not an upstream
            # auth/rate-limit issue — preserve the pre-existing "fall back
            # to empty" behaviour so a machine without ``gh`` does not spam
            # ScannerError per tick.
            return []
        if rc != 0:
            error_class = classify_gh_stderr(err)
            detail = f"gh pr list {slug!r} rc={rc}: {err.strip()[:200]}"
            raise ScannerError(
                scanner="pr_sweep",
                error_class=error_class,
                detail=detail,
            )
        if not out.strip():
            return []
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []
        return [_decode_pr(slug=slug, raw=cast("GhPrJson", item)) for item in data if isinstance(item, dict)]

    def main_check_failed(self, *, slug: str, check_name: str) -> bool:
        argv = [
            "api",
            f"repos/{slug}/commits/main/check-runs",
            "--jq",
            f'.check_runs | map(select(.name == "{check_name}")) | .[0].conclusion // ""',
        ]
        rc, out, _ = self._run_gh(argv)
        if rc != 0:
            return False
        return out.strip().lower() not in {"success", "neutral", "skipped", ""}

    def merge_pr_squash_bound(self, *, slug: str, pr_id: int, expected_head_oid: str) -> tuple[bool, str]:  # noqa: PLR6301 — PrApiClient port; the bound merge is a stateless keystone delegate.
        """SHA-bound squash merge (#1985) — delegates to the keystone primitive.

        Replaces the former unbound ``gh pr merge --squash``: ``execute_bound_merge``
        binds the merge to ``expected_head_oid`` so a force-push in the TOCTOU
        window is rejected (the §17.4.3 SHA-bind), runs the transient-retry +
        head-moved classification, and never merges an unreviewed head. A merge
        precondition failure (head moved, policy refusal, transient exhaustion)
        returns ``(False, "")`` to preserve the caller's ``(ok, sha)`` contract.
        """
        from teatree.core.merge import MergePreconditionError, execute_bound_merge  # noqa: PLC0415

        try:
            merged_sha = execute_bound_merge(slug=slug, pr_id=pr_id, expected_head_oid=expected_head_oid)
        except MergePreconditionError:
            return False, ""
        return True, merged_sha

    def _run_gh(self, argv: list[str]) -> tuple[int, str, str]:
        gh = shutil.which("gh") or "gh"
        env = {**os.environ, "GH_TOKEN": self.token} if self.token else None
        try:
            result = run_allowed_to_fail([gh, *argv], expected_codes=None, env=env)
        except FileNotFoundError:
            return 127, "", "gh not installed"
        return result.returncode, result.stdout, result.stderr


@dataclass(slots=True)
class CallCommandMergeKeystone:
    """Production :class:`MergeKeystone` — invokes ``call_command('ticket', 'merge', …)``."""

    loop_identity: str = "merge-loop"

    def merge_clear(self, *, clear_id: int) -> tuple[bool, str, str, str]:
        from django.core.management import call_command  # noqa: PLC0415

        result = call_command("ticket", "merge", str(clear_id), loop_identity=self.loop_identity)
        if not isinstance(result, dict):
            return False, "", "ticket merge returned non-dict", ""
        merged = bool(result.get("merged"))
        merged_sha = str(result.get("merged_sha") or "")
        error = str(result.get("error") or "")
        escalation_kind = str(result.get("escalation_kind") or "")
        return merged, merged_sha, error, escalation_kind


@dataclass(slots=True)
class AutoReviewTaskDispatcher:
    """Production :class:`ReviewDispatcher` — records the dedup ledger + reviewing task (#68)."""

    def enqueue(  # noqa: PLR6301 — instance method to satisfy the injected ReviewDispatcher Protocol (mirrors sibling port adapters).
        self, *, slug: str, pr_id: int, head_sha: str, pr_url: str, overlay: str
    ) -> bool:
        from teatree.core.models.auto_review_dispatch import AutoReviewDispatch  # noqa: PLC0415

        row = AutoReviewDispatch.enqueue(
            slug=slug,
            pr_id=pr_id,
            head_sha=head_sha,
            pr_url=pr_url,
            overlay=overlay,
        )
        return row is not None


@dataclass(slots=True)
class SlackMergeNotifier:
    """Post a one-line DM on every actual merge, and on a flag-level signal."""

    backend: object
    user_id: str = ""

    def announce(self, *, slug: str, pr_id: int, merged_sha: str, fallback: bool) -> None:
        prefix = "merged (uv-audit fallback)" if fallback else "merged"
        sha_short = merged_sha[:8] if merged_sha else "?"
        self._post(f"{prefix} {slug}#{pr_id} @ {sha_short}")

    def flag(self, *, slug: str, pr_id: int, reason: str, url: str) -> None:
        target = url or f"{slug}#{pr_id}"
        if reason == _MERGEABLE_AWAITING_REVIEW_REASON:
            self._post(f"mergeable, ready to request review {target}")
            return
        self._post(f"flag ({reason}) {target}")

    def _post(self, text: str) -> None:
        if not self.user_id:
            return
        post = getattr(self.backend, "post_dm", None) or getattr(self.backend, "post_message", None)
        if not callable(post):
            return
        post(channel=self.user_id, text=text)


@dataclass(slots=True)
class NullMergeNotifier:
    """No-op notifier — used when Slack is not configured for the overlay."""

    calls: list[tuple[str, int, str, bool]] = field(default_factory=list)
    flag_calls: list[tuple[str, int, str, str]] = field(default_factory=list)

    def announce(self, *, slug: str, pr_id: int, merged_sha: str, fallback: bool) -> None:
        self.calls.append((slug, pr_id, merged_sha, fallback))

    def flag(self, *, slug: str, pr_id: int, reason: str, url: str) -> None:
        self.flag_calls.append((slug, pr_id, reason, url))
