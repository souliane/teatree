"""Scan PRs awaiting review from the active user.

Maintains a per-PR ``last_reviewed_sha`` cache so the dispatcher only
fires the reviewer phase agent when the PR has new commits since the
last review pass, OR when the reviewer's prior approval was dismissed
(e.g. invalidated on force-push, re-requested after a dismissal).

The cache values are ``{"sha": ..., "state": ...}`` dicts. Legacy string
values are read transparently as ``{"sha": value, "state": ""}`` and
rewritten in the new shape on the next update — no migration command
required.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from teatree.backends.protocols import CodeHostBackend, ReviewState
from teatree.loop.scanners.base import ScanSignal
from teatree.paths import DATA_DIR
from teatree.types import RawAPIDict


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """One cached observation per PR — head sha and last known review state."""

    sha: str = ""
    state: str = ""


def _default_cache_path() -> Path:
    return DATA_DIR / "loop" / "reviewer_prs.json"


def _read_cache(path: Path) -> dict[str, CacheEntry]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, CacheEntry] = {}
    for key, value in data.items():
        if isinstance(value, str):
            result[str(key)] = CacheEntry(sha=value, state="")
        elif isinstance(value, dict):
            sha = value.get("sha")
            state = value.get("state")
            result[str(key)] = CacheEntry(
                sha=sha if isinstance(sha, str) else "",
                state=state if isinstance(state, str) else "",
            )
    return result


def _write_cache(path: Path, data: dict[str, CacheEntry]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialised = {url: {"sha": entry.sha, "state": entry.state} for url, entry in data.items()}
    path.write_text(json.dumps(serialised, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _head_sha(pr: RawAPIDict) -> str:
    sha = pr.get("sha")
    if isinstance(sha, str):
        return sha
    head = pr.get("head")
    if isinstance(head, dict):
        head_dict = cast("RawAPIDict", head)
        head_sha = head_dict.get("sha")
        if isinstance(head_sha, str):
            return head_sha
    diff_refs = pr.get("diff_refs")
    if isinstance(diff_refs, dict):
        diff_dict = cast("RawAPIDict", diff_refs)
        head_sha = diff_dict.get("head_sha")
        if isinstance(head_sha, str):
            return head_sha
    return ""


def _pr_url(pr: RawAPIDict) -> str:
    for name in ("web_url", "html_url"):
        value = pr.get(name)
        if isinstance(value, str):
            return value
    return ""


def _is_dismissed_from_approved(previous: str, current: ReviewState) -> bool:
    """Did the reviewer's prior APPROVED status get invalidated?

    A dismissal is any transition from a recorded ``approved`` state to a
    state where the approval no longer counts: ``DISMISSED`` (explicit) or
    ``PENDING`` (re-requested / dropped on force-push).
    """
    return previous == ReviewState.APPROVED.value and current in {ReviewState.DISMISSED, ReviewState.PENDING}


@dataclass(slots=True)
class ReviewerPrsScanner:
    """Lists PRs where the active user is a requested reviewer.

    Emits ``reviewer_pr.new_sha`` for any PR whose head sha has changed
    since the last cached review pass; ``reviewer_pr.unreviewed`` for
    first-time observations; ``reviewer_pr.approval_dismissed`` when the
    reviewer's prior approval was dropped (forge invalidated it on
    force-push, or the author re-requested review after a dismissal).
    """

    host: CodeHostBackend
    cache_path: Path = field(default_factory=_default_cache_path)
    name: str = "reviewer_prs"

    def scan(self) -> list[ScanSignal]:
        reviewer = self.host.current_user()
        if not reviewer:
            return []
        prs = self.host.list_review_requested_prs(reviewer=reviewer)
        cache = _read_cache(self.cache_path)
        signals: list[ScanSignal] = []
        updates: dict[str, CacheEntry] = {}
        for pr in prs:
            url = _pr_url(pr)
            if not url:
                continue
            head = _head_sha(pr)
            previous = cache.get(url, CacheEntry())
            if previous.sha and previous.sha != head:
                signals.append(
                    ScanSignal(
                        kind="reviewer_pr.new_sha",
                        summary=f"Review needed: {url}",
                        payload={"url": url, "head_sha": head, "previous_sha": previous.sha, "raw": pr},
                    )
                )
                updates[url] = CacheEntry(sha=head, state=previous.state)
                continue
            if not previous.sha:
                signals.append(
                    ScanSignal(
                        kind="reviewer_pr.unreviewed",
                        summary=f"Review needed: {url}",
                        payload={"url": url, "head_sha": head, "previous_sha": "", "raw": pr},
                    )
                )
                continue
            current = self.host.get_review_state(pr_url=url, reviewer=reviewer)
            if _is_dismissed_from_approved(previous.state, current):
                signals.append(
                    ScanSignal(
                        kind="reviewer_pr.approval_dismissed",
                        summary=f"Approval dismissed: {url}",
                        payload={
                            "url": url,
                            "head_sha": head,
                            "previous_state": previous.state,
                            "current_state": current.value,
                            "raw": pr,
                        },
                    )
                )
            if current.value != previous.state:
                updates[url] = CacheEntry(sha=previous.sha, state=current.value)
        if updates:
            cache.update(updates)
            _write_cache(self.cache_path, cache)
        return signals

    def mark_reviewed(self, *, url: str, sha: str, state: str = "") -> None:
        """Record that *url* has been reviewed at *sha*; called by the dispatcher."""
        mark_reviewed(url=url, sha=sha, state=state, cache_path=self.cache_path)


def mark_reviewed(*, url: str, sha: str, state: str = "", cache_path: Path | None = None) -> None:
    """Module-level entry point to update the reviewer cache without owning a scanner instance.

    Called from ``Ticket.mark_reviewed_externally`` when a reviewer-role
    ticket's reviewing task completes — the model layer should not
    instantiate a backend just to write one JSON file. ``state`` defaults
    to ``"approved"`` when not supplied so the next scan can detect a
    dismissal of the recorded approval.
    """
    path = cache_path or _default_cache_path()
    cache = _read_cache(path)
    resolved_state = state or ReviewState.APPROVED.value
    cache[url] = CacheEntry(sha=sha, state=resolved_state)
    _write_cache(path, cache)
