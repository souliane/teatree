"""Scan PRs awaiting review from the active user.

Maintains a per-PR ``last_reviewed_sha`` cache so the dispatcher only
fires the reviewer phase agent when the PR has new commits since the
last review pass.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from teatree.backends.protocols import CodeHostBackend
from teatree.config import DATA_DIR
from teatree.core.sync import RawAPIDict
from teatree.loop.scanners.base import ScanSignal


def _default_cache_path() -> Path:
    return DATA_DIR / "loop" / "reviewer_prs.json"


def _read_cache(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items() if isinstance(value, str)}


def _write_cache(path: Path, data: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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


@dataclass(slots=True)
class ReviewerPrsScanner:
    """Lists PRs where the active user is a requested reviewer.

    Emits ``reviewer_pr.new_sha`` for any PR whose head sha has changed
    since the last cached review pass; ``reviewer_pr.unreviewed`` for
    first-time observations.
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
        for pr in prs:
            url = _pr_url(pr)
            if not url:
                continue
            head = _head_sha(pr)
            previous = cache.get(url, "")
            kind = "reviewer_pr.new_sha" if previous and previous != head else "reviewer_pr.unreviewed"
            if previous == head:
                continue
            signals.append(
                ScanSignal(
                    kind=kind,
                    summary=f"Review needed: {url}",
                    payload={"url": url, "head_sha": head, "previous_sha": previous, "raw": pr},
                )
            )
        return signals

    def mark_reviewed(self, *, url: str, sha: str) -> None:
        """Record that *url* has been reviewed at *sha*; called by the dispatcher."""
        cache = _read_cache(self.cache_path)
        cache[url] = sha
        _write_cache(self.cache_path, cache)
