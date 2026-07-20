"""Scan PRs the active user has open across configured code-host repos."""

import logging
from dataclasses import dataclass, field
from typing import cast

from teatree.core.backend_protocols import CodeHostBackend
from teatree.loop.scanners.base import ScanSignal, SignalPayload
from teatree.loop.scanners.pr_payload import head_sha
from teatree.loop.url_specificity import best_url_match_specificity
from teatree.types import RawAPIDict
from teatree.utils.throttled_log import warn_throttled

logger = logging.getLogger(__name__)

# The keys any forge populates with a pipeline/CI status. A PR carrying NONE of
# them was never enriched — its red-pipeline lane is structurally inert, which
# the scanner surfaces (throttled) instead of silently reading "".
_PIPELINE_FIELDS = ("head_pipeline", "status_check_rollup", "mergeable_state")


def _has_pipeline_field(pr: RawAPIDict) -> bool:
    return any(name in pr for name in _PIPELINE_FIELDS)


def _str_field(data: RawAPIDict, *names: str) -> str:
    for name in names:
        value = data.get(name)
        if isinstance(value, str):
            return value
    return ""


def _int_field(data: RawAPIDict, *names: str) -> int:
    for name in names:
        value = data.get(name)
        if isinstance(value, int):
            return value
    return 0


# A pipeline is green only when it explicitly succeeded.
_GREEN_STATUSES = {"success", "succeeded", "passed"}

# Legitimately still in progress — not green yet, but not red either. Blank
# ("") means no pipeline has started; treat that as not-yet-running, not a
# failure (a brand-new PR or a no-CI repo shouldn't spam action-needed).
_IN_PROGRESS_STATUSES = {
    "running",
    "pending",
    "created",
    "preparing",
    "waiting_for_resource",
    "scheduled",
    "",
}


def _needs_attention(status: str) -> bool:
    """Not-green == red.

    Any pipeline state that is neither an explicit success nor a
    legitimately-in-progress state — ``failed``/``error``/``canceled``/
    ``skipped``/``manual``/``blocked``/any unknown terminal value — must
    surface as action-needed. The old code only treated three literals
    (``failed``/``failure``/``error``) as failure and silently passed
    everything else (gray/skipped/manual/canceled) as a benign open PR;
    that is the "walked away from a gray job" failure mode this fixes.
    """
    return status not in _GREEN_STATUSES and status not in _IN_PROGRESS_STATUSES


def _pipeline_status(pr: RawAPIDict) -> str:
    """Return the most relevant pipeline state across host shapes.

    GitLab MRs expose ``head_pipeline.status``; GitHub PRs expose a
    nested ``status_check_rollup`` or ``mergeable_state``. Scanners
    surface whatever the backend chose to populate; missing data is "".
    """
    pipeline = pr.get("head_pipeline")
    if isinstance(pipeline, dict):
        status = cast("RawAPIDict", pipeline).get("status")
        if isinstance(status, str):
            return status
    rollup = pr.get("status_check_rollup")
    if isinstance(rollup, dict):
        state = cast("RawAPIDict", rollup).get("state")
        if isinstance(state, str):
            return state
    state = pr.get("mergeable_state")
    return state if isinstance(state, str) else ""


@dataclass(slots=True)
class MyPrsScanner:
    """Lists open PRs authored by the active user.

    Returns a ``my_pr.failed`` signal when the head pipeline is in a
    failed state, ``my_pr.draft_notes`` when there are pending review
    comments to address, and ``my_pr.open`` for every other open PR so
    the dispatcher can render an "in flight" summary.

    ``identities`` opts the scanner into a multi-alias union query — used
    when the user has more than one identity on the same forge (a personal
    login plus an org-account login under one PAT scope). When empty the
    scanner falls back to ``host.current_user()`` so legacy single-identity
    setups behave unchanged. PRs surfaced under multiple aliases are
    deduped by ``url`` (#976).

    ``allowed_url_prefixes`` gates emission to PRs whose URL starts with
    one of the listed prefixes. A scanner registered for an overlay should
    pass its workspace-repo URL prefixes here so PRs from sibling overlays
    sharing the same code-host token don't bleed into this overlay's
    statusline zone (#1015). Empty tuple keeps the legacy "emit all"
    behaviour for callers that scan a single global account.

    ``competing_url_prefixes`` carries the URL-prefix claims of OTHER
    registered overlays (#1324). When a PR's URL is claimed by both this
    overlay and another, the most-specific claim wins — a wildcard prefix
    like ``host/*/repo/`` loses to an exact ``host/owner/repo/`` claim, so
    a teatree-overlay dogfooding scan that lists ``souliane/teatree`` plus
    a sibling overlay's repo path does not steal the sibling's PRs from
    its own zone. Empty tuple disables cross-overlay attribution.
    """

    host: CodeHostBackend
    identities: tuple[str, ...] = field(default_factory=tuple)
    allowed_url_prefixes: tuple[str, ...] = field(default_factory=tuple)
    competing_url_prefixes: tuple[str, ...] = field(default_factory=tuple)
    name: str = "my_prs"

    def scan(self) -> list[ScanSignal]:
        authors = self._resolve_identities()
        if not authors:
            return []
        prs = self._collect_unique_prs(authors)
        signals: list[ScanSignal] = []
        unenriched = 0
        for pr in prs:
            url = _str_field(pr, "web_url", "html_url")
            if not self._url_allowed(url):
                continue
            if not _has_pipeline_field(pr):
                # No pipeline field at all — the backend never populated CI state,
                # so my_pr.failed can't fire for this PR. Count it and warn once
                # per tick rather than silently classifying it as a benign open PR.
                unenriched += 1
            title = _str_field(pr, "title")
            iid = _int_field(pr, "iid", "number")
            status = _pipeline_status(pr)
            base_payload: SignalPayload = {
                "url": url,
                "title": title,
                "iid": iid,
                "status": status,
                # Carried on EVERY signal so ``my_pr.failed`` reaches
                # ``claim_red_mr_fix`` with a real head sha — the RedMrFixAttempt
                # ledger stayed empty (#7) while this was omitted.
                "head_sha": head_sha(pr),
                "raw": pr,
            }
            if _needs_attention(status):
                signals.append(
                    ScanSignal(
                        kind="my_pr.failed",
                        summary=f"PR #{iid} pipeline {status or 'no-status'} (not green): {title}",
                        payload=base_payload,
                    )
                )
                continue
            draft_count = _int_field(pr, "user_notes_count", "review_comments")
            if draft_count > 0 and status != "success":
                signals.append(
                    ScanSignal(
                        kind="my_pr.draft_notes",
                        summary=f"PR #{iid} has {draft_count} unresolved notes: {title}",
                        payload={**base_payload, "draft_count": draft_count},
                    )
                )
                continue
            signals.append(
                ScanSignal(
                    kind="my_pr.open",
                    summary=f"PR #{iid} {status or 'open'}: {title}",
                    payload=base_payload,
                )
            )
        if unenriched:
            warn_throttled(
                logger,
                f"my_prs-unenriched:{self.name}",
                "%s: %d open PR(s) carry no pipeline field — the my_pr.failed auto-debug lane is inert for them; "
                "the code host did not populate CI status",
                self.name,
                unenriched,
            )
        return signals

    def _url_allowed(self, url: str) -> bool:
        """Drop a PR whose URL is outside the overlay's repo prefixes (#1015, #1324).

        When ``allowed_url_prefixes`` is empty the scanner is single-overlay
        (or legacy multi-overlay) and emits every PR it sees. When non-empty,
        only URLs claimed by one of the prefixes survive — sibling MRs from
        another overlay's repos are dropped at the scanner boundary so they
        never reach the per-overlay statusline zone.

        Prefix shapes are interpreted by
        :func:`teatree.loop.url_specificity.url_match_specificity` — plain
        prefixes match ``startswith``, wildcard ``host/*/repo/`` prefixes
        match across any owner segment (#1324).

        When a competing overlay's claim is **more specific** (longer
        non-wildcard prefix) than every claim this scanner holds, the URL
        is dropped so the sibling overlay's scanner emits the PR under its
        own ``[overlay]`` zone instead of this scanner stealing it (#1324).
        """
        if not self.allowed_url_prefixes:
            return True
        if not url:
            return False
        own = best_url_match_specificity(url, self.allowed_url_prefixes)
        if own == 0:
            return False
        competing = best_url_match_specificity(url, self.competing_url_prefixes)
        return competing <= own

    def _resolve_identities(self) -> tuple[str, ...]:
        # Multi-identity wins: caller supplied an explicit alias set, use it
        # verbatim so a misconfigured ``current_user`` (wrong PAT scope, or a
        # token whose `/user` differs from the human's preferred handle)
        # doesn't silently re-collapse the query. Empty falls back to the
        # legacy single-user contract.
        if self.identities:
            return tuple(dict.fromkeys(self.identities))
        user = self.host.current_user()
        return (user,) if user else ()

    def _collect_unique_prs(self, authors: tuple[str, ...]) -> list[RawAPIDict]:
        """Union PRs across *authors*, deduped by URL.

        A PR returned for two aliases (co-author / shared identity) renders
        once. PRs without a URL keep their legacy "emit once" shape — there
        is no other stable identity to dedup on.
        """
        seen_urls: set[str] = set()
        prs: list[RawAPIDict] = []
        for author in authors:
            for pr in self.host.list_my_prs(author=author):
                url = _str_field(pr, "web_url", "html_url")
                if url and url in seen_urls:
                    continue
                if url:
                    seen_urls.add(url)
                prs.append(pr)
        return prs
