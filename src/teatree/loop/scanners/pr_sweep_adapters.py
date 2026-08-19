"""Production I/O adapters for :class:`teatree.loop.scanners.pr_sweep.PrSweepScanner`.

The scanner core (decision ladder, signals) lives in ``pr_sweep``; this module
holds the side-effecting implementations of its three injected ports — the
``gh``-backed :class:`PrApiClient`, the ``call_command`` :class:`MergeKeystone`,
and the Slack :class:`MergeNotifier` (plus a null notifier) — together with the
``gh pr list --json`` decoding. Splitting the adapters out keeps the scanner
module focused on logic and under the module-health LOC cap.
"""

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypedDict, cast

from teatree.loop.main_check_runs import check_runs_argv, parse_check_run_pages
from teatree.loop.scanners.base import ScannerError, classify_gh_stderr
from teatree.loop.scanners.pr_sweep import GH_CONFLICT_MERGE_STATE, GH_CONFLICT_MERGEABLE, PrSummary
from teatree.loop.scanners.pr_sweep_types import CLEAR_PRESENT_UNUSABLE_REASON as _CLEAR_PRESENT_UNUSABLE_REASON
from teatree.loop.scanners.pr_sweep_types import CONTESTED_HOLD_REASON as _CONTESTED_HOLD_REASON
from teatree.loop.scanners.pr_sweep_types import HOLD_AT_HEAD_REASON as _HOLD_AT_HEAD_REASON
from teatree.loop.scanners.pr_sweep_types import MERGEABLE_AWAITING_REVIEW_REASON as _MERGEABLE_AWAITING_REVIEW_REASON
from teatree.utils.pr_ref import PrRef
from teatree.utils.run import run_allowed_to_fail

if TYPE_CHECKING:
    from teatree.core.backend_protocols import MessagingBackend
    from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)

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
    isCrossRepository: bool
    baseRefName: str
    baseRefOid: str


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


def _decode_pr(*, slug: str, raw: GhPrJson, base_head_sha: str) -> PrSummary:
    """Decode one ``gh pr list --json`` entry.

    *base_head_sha* is the live head of this PR's base branch, resolved once per
    ``(repo, base branch)`` by :meth:`GhPrApiClient._decode_all`. It is the second
    of the two commits behind-ness is a property of — see :func:`_gh_is_behind_main`.
    """
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
    cross_repo = raw.get("isCrossRepository")
    same_repo = (not cross_repo) if isinstance(cross_repo, bool) else None
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
        behind_main=_gh_is_behind_main(raw, base_head_sha=base_head_sha),
        author=_author_login(raw),
        same_repo=same_repo,
    )


def _gh_is_behind_main(raw: GhPrJson, *, base_head_sha: str) -> bool:
    """True iff the base branch has advanced past this PR's merge base (#4526).

    Behind-ness is a property of TWO COMMITS — the base branch's live head and the
    merge base the PR is built on — so it is read from those, never inferred from
    ``mergeStateStatus``. That field carries a SINGLE value chosen by precedence
    (``DIRTY`` > ``BLOCKED`` > ``BEHIND``), not a set of flags: a PR that is behind
    AND has failing required checks reports ``BLOCKED``, and one that is behind AND
    conflicted reports ``DIRTY``. It reports ``BEHIND`` only when the branch is
    behind with nothing else wrong.

    Testing ``== "BEHIND"`` therefore answered ``False`` for precisely the PR the
    stale-base remedy exists to repair — one whose required checks went red because
    its base moved, which always reports ``BLOCKED``. The precondition was mutually
    exclusive with the condition being repaired, and
    :func:`~teatree.loop.scanners.pr_sweep_branch_update.remedy_stale_base` never
    ran once (zero ``BranchUpdateAttempt`` rows since #4063 shipped).

    ``baseRefOid`` is the PR's MERGE BASE — verified against
    ``/repos/{slug}/compare/{base}...{head}``, where it equals
    ``merge_base_commit.sha`` and NOT the base head — so behind-ness needs no
    per-PR round trip: it rides in the ``gh pr list --json`` payload the sweep
    already fetches, against one base-head read per ``(repo, base branch)`` tick.

    An UNKNOWN comparison — either SHA missing, or the base-head read having
    failed — is never reported as "not behind". Reporting an unknown as a verdict
    is what silently skips the remedy; this fails closed to ``True``, so the PR is
    repaired or flagged, never dropped. A conflicted PR is behind like any other;
    refusing to merge-update it is the separate ``is_conflicted`` bound the
    scanner ladder and ``remedy_stale_base`` both enforce.
    """
    merge_base = _as_str(raw.get("baseRefOid")).strip().lower()
    base_head = base_head_sha.strip().lower()
    if not merge_base or not base_head:
        return True
    return merge_base != base_head


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
            (
                "number,headRefOid,isDraft,url,title,reviews,statusCheckRollup,mergeable,mergeStateStatus,author,"
                "isCrossRepository,baseRefName,baseRefOid"
            ),
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
        return self._decode_all(slug=slug, entries=[cast("GhPrJson", item) for item in data if isinstance(item, dict)])

    def _decode_all(self, *, slug: str, entries: list[GhPrJson]) -> list[PrSummary]:
        """Decode every listed PR, resolving each distinct base branch's head ONCE.

        Behind-ness compares the PR's merge base (free, in the payload) against its
        base branch's live head. The head is memoised per ``baseRefName``, so a repo
        whose open PRs all target ``main`` costs ONE extra call per tick — not one
        per PR per tick, which is what a ``/compare`` call per PR would have cost.
        """
        heads: dict[str, str] = {}
        decoded: list[PrSummary] = []
        for raw in entries:
            base_ref = _as_str(raw.get("baseRefName"))
            if base_ref not in heads:
                heads[base_ref] = self._branch_head_sha(slug=slug, ref=base_ref)
            decoded.append(_decode_pr(slug=slug, raw=raw, base_head_sha=heads[base_ref]))
        return decoded

    def _branch_head_sha(self, *, slug: str, ref: str) -> str:
        """The live head SHA of ``refs/heads/{ref}``, or ``""`` when it cannot be read.

        Every failure mode — an absent ``baseRefName``, a non-zero ``gh`` exit, an
        unparsable or unexpected body — returns ``""``, which :func:`_gh_is_behind_main`
        reads as UNKNOWN and fails closed to "behind". A read failure must never be
        reported as "not behind": that is the silent-skip this whole fix removes.
        A missing head is logged, never raised, so one unreadable branch cannot abort
        the tick for the PRs the sweep CAN judge.
        """
        if not ref:
            return ""
        rc, out, err = self._run_gh(["api", f"repos/{slug}/git/ref/heads/{ref}"])
        if rc != 0:
            logger.warning("pr_sweep could not read %s head of %r rc=%d: %s", ref, slug, rc, err.strip()[:200])
            return ""
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            logger.warning("pr_sweep got an unparsable %s head payload for %r", ref, slug)
            return ""
        obj = payload.get("object") if isinstance(payload, dict) else None
        sha = _as_str(obj.get("sha")) if isinstance(obj, dict) else ""
        if not sha:
            logger.warning("pr_sweep got no head sha for %s of %r", ref, slug)
        return sha

    def main_check_failed(self, *, slug: str, check_name: str) -> bool:
        """Whether *check_name* has a completed, non-green conclusion on ``main`` (#4090 sibling).

        Reads across ALL pages via the shared :mod:`teatree.loop.main_check_runs` reader —
        an unpaginated single-page read sees only GitHub's first 30 check-runs, so a named
        check landing past page 1 would read as absent and this would report "not failed"
        even when ``main`` genuinely is red on it. Any read failure (non-zero ``gh`` exit,
        unparsable pages, the check absent from every page) degrades to ``False`` — the
        existing fail-toward-"not confirmed failed" direction the uv-audit fallback caller
        already treats as safe.
        """
        argv = check_runs_argv(slug=slug, ref="main")
        rc, out, _ = self._run_gh(argv)
        if rc != 0:
            return False
        runs = parse_check_run_pages(out)
        if runs is None:
            return False
        match = next((run for run in runs if str(run.get("name") or "") == check_name), None)
        if match is None:
            return False
        conclusion = str(match.get("conclusion") or "").strip().lower()
        return conclusion not in {"success", "neutral", "skipped", ""}

    def merge_pr_squash_bound(self, *, slug: str, pr_id: int, expected_head_oid: str) -> tuple[bool, str]:  # noqa: PLR6301 — PrApiClient port; the bound merge is a stateless keystone delegate.
        """SHA-bound squash merge (#1985) — delegates to the keystone primitive.

        Replaces the former unbound ``gh pr merge --squash``: ``execute_bound_merge``
        binds the merge to ``expected_head_oid`` so a force-push in the TOCTOU
        window is rejected (the §17.4.3 SHA-bind), runs the transient-retry +
        head-moved classification, and never merges an unreviewed head. A merge
        precondition failure (head moved, policy refusal, transient exhaustion)
        returns ``(False, "")`` to preserve the caller's ``(ok, sha)`` contract.
        """
        from teatree.core.merge import MergePreconditionError, execute_bound_merge  # noqa: PLC0415 — tick-time import

        try:
            merged_sha = execute_bound_merge(ref=PrRef(slug=slug, pr_id=pr_id), expected_head_oid=expected_head_oid)
        except MergePreconditionError:
            return False, ""
        return True, merged_sha

    def update_pr_branch(self, *, slug: str, pr_id: int, expected_head_oid: str) -> bool:
        """Merge the base into the PR branch, bound to *expected_head_oid* (#4063).

        The SHA-bound sibling of :meth:`merge_pr_squash_bound`: GitHub refuses the
        update (422) when the live head is no longer *expected_head_oid*, so a
        force-push in the TOCTOU window between the sweep's snapshot and this call
        can never merge into a head the sweep did not judge. Returns ``False`` on
        any non-zero rc (conflict, revoked permission, rate limit) — the caller
        degrades to the ``needs_branch_update`` flag rather than retrying.
        """
        argv = [
            "api",
            "--method",
            "PUT",
            f"repos/{slug}/pulls/{pr_id}/update-branch",
            "-f",
            f"expected_head_sha={expected_head_oid}",
        ]
        rc, _out, err = self._run_gh(argv)
        if rc != 0:
            logger.warning("pr_sweep update-branch refused for %s#%d rc=%d: %s", slug, pr_id, rc, err.strip()[:200])
        return rc == 0

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

    def merge_clear(self, *, clear_id: int, human_authorized: str = "") -> tuple[bool, str, str, str, str]:
        from django.core.management import call_command  # noqa: PLC0415 — deferred: Django import at call time

        # #3413: thread the standing substrate authorizer through as the same
        # ``--human-authorized`` an interactive substrate merge presents. Empty (the
        # default) reproduces the prior loop-driven merge exactly.
        result = call_command(
            "ticket", "merge", str(clear_id), loop_identity=self.loop_identity, human_authorized=human_authorized
        )
        if not isinstance(result, dict):
            return False, "", "ticket merge returned non-dict", "", ""
        merged = bool(result.get("merged"))
        merged_sha = str(result.get("merged_sha") or "")
        error = str(result.get("error") or "")
        escalation_kind = str(result.get("escalation_kind") or "")
        standing_delegation_by = str(result.get("standing_delegation_by") or "")
        return merged, merged_sha, error, escalation_kind, standing_delegation_by


@dataclass(slots=True)
class AutoReviewTaskDispatcher:
    """Production :class:`ReviewDispatcher` — records the dedup ledger + reviewing task (#68)."""

    def enqueue(  # noqa: PLR6301 — instance method to satisfy the injected ReviewDispatcher Protocol (mirrors sibling port adapters).
        self, *, slug: str, pr_id: int, head_sha: str, pr_url: str, overlay: str
    ) -> bool:
        from teatree.core.models.auto_review_dispatch import AutoReviewDispatch  # noqa: PLC0415 — lazy ORM import

        row = AutoReviewDispatch.enqueue(
            slug=slug,
            pr_id=pr_id,
            head_sha=head_sha,
            pr_url=pr_url,
            overlay=overlay,
        )
        return row is not None


#: Flag reasons the owner is DM'd about instead of only logged — a condition no
#: further tick can clear on its own. The BotPing ledger still caps each at one DM
#: per ``(repo, PR, reason)``, so escalating cannot reintroduce per-tick spam.
OWNER_ESCALATION_FLAG_REASONS: frozenset[str] = frozenset(
    {
        _CLEAR_PRESENT_UNUSABLE_REASON,
        _CONTESTED_HOLD_REASON,
        _HOLD_AT_HEAD_REASON,
    }
)

_HELD_REMEDY = (
    "the auto-merge is refused until the holding reviewer lifts it, a CLEAR is issued at that SHA, "
    "or a new commit moves the head"
)

_FLAG_TEXTS: dict[str, str] = {
    _MERGEABLE_AWAITING_REVIEW_REASON: "mergeable, ready to request review",
    _CLEAR_PRESENT_UNUSABLE_REASON: (
        "a CLEAR exists for this PR but does not authorise its live head — re-issue at the current SHA"
    ),
    _CONTESTED_HOLD_REASON: (
        f"two cold reviews disagree at this PR's live head — a HOLD stands that no one took back, so {_HELD_REMEDY}"
    ),
    _HOLD_AT_HEAD_REASON: (
        f"a cold review returned HOLD at this PR's live head and nobody took it back, so {_HELD_REMEDY}"
    ),
}


@dataclass(slots=True)
class SlackMergeNotifier:
    """Route merge announcements + flag signals through the notify-relevance policy.

    :meth:`announce` is an OWNER_DELIVERY (a PR merged) — DM'd exactly once per
    merge via the :class:`~teatree.core.models.BotPing` idempotency ledger keyed
    on the merged SHA. :meth:`flag` is INTERNAL by default (the loop re-flags every
    un-reviewed PR each ~5-minute tick) — logged only, never DM'd, so re-flagging
    the same stuck PR forever can never spam the owner. This replaces the former
    raw ``backend.post_message`` bypass that DM'd on every tick per open PR (F1).

    :data:`OWNER_ESCALATION_FLAG_REASONS` names the flags that must not stay
    log-only: a condition nothing in the loop can clear on its own needs the owner,
    and the ledger still caps it at one DM per ``(repo, PR, reason)``.
    """

    backend: object
    user_id: str = ""

    def announce(self, *, slug: str, pr_id: int, merged_sha: str, fallback: bool) -> None:
        from teatree.core.modelkit.notify_policy import NotifyAudience  # noqa: PLC0415 — tick-time import, kept lazy
        from teatree.core.notify import NotifyKind, notify_user  # noqa: PLC0415 — tick-time import, kept lazy

        prefix = "merged (uv-audit fallback)" if fallback else "merged"
        sha_short = merged_sha[:8] if merged_sha else "?"
        notify_user(
            f"{prefix} {slug}#{pr_id} @ {sha_short}",
            kind=NotifyKind.INFO,
            idempotency_key=f"merge-announce:{slug}#{pr_id}:{merged_sha}",
            audience=NotifyAudience.OWNER_DELIVERY,
            backend=cast("MessagingBackend", self.backend) if self.backend is not None else None,
            user_id=self.user_id or None,
        )

    def flag(self, *, slug: str, pr_id: int, reason: str, url: str, detail: str = "") -> None:  # noqa: PLR6301 — instance method satisfies the injected MergeNotifier Protocol (mirrors sibling adapters).
        from teatree.core.modelkit.notify_policy import NotifyAudience  # noqa: PLC0415 — tick-time import, kept lazy
        from teatree.core.notify import NotifyKind, notify_user  # noqa: PLC0415 — tick-time import, kept lazy

        target = url or f"{slug}#{pr_id}"
        suffix = f" ({detail})" if detail else ""
        text = _FLAG_TEXTS.get(reason, f"flag ({reason})") + f" {target}{suffix}"
        notify_user(
            text,
            kind=NotifyKind.INFO,
            idempotency_key=f"pr-sweep-flag:{slug}#{pr_id}:{reason}",
            audience=(
                NotifyAudience.OWNER_ESCALATION if reason in OWNER_ESCALATION_FLAG_REASONS else NotifyAudience.INTERNAL
            ),
        )


@dataclass(slots=True)
class NullMergeNotifier:
    """No-op notifier — used when Slack is not configured for the overlay."""

    calls: list[tuple[str, int, str, bool]] = field(default_factory=list)
    flag_calls: list[tuple[str, int, str, str]] = field(default_factory=list)
    flag_details: list[str] = field(default_factory=list)

    def announce(self, *, slug: str, pr_id: int, merged_sha: str, fallback: bool) -> None:
        self.calls.append((slug, pr_id, merged_sha, fallback))

    def flag(self, *, slug: str, pr_id: int, reason: str, url: str, detail: str = "") -> None:
        self.flag_calls.append((slug, pr_id, reason, url))
        self.flag_details.append(detail)
