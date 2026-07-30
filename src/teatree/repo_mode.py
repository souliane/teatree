"""Solo vs collaborative repo-mode detection (issue #550 item 4).

One heuristic, one source of truth: ``git shortlog -sn --no-merges`` over a
trailing window on the default branch. If the top author owns at least
``solo_threshold`` of the windowed commits the repo is ``SOLO`` (the agent
may fix unrelated issues proactively); otherwise ``COLLABORATIVE`` (flag,
don't fix — someone else owns that code). An empty window is treated as
``COLLABORATIVE``: unknown authorship is the conservative default, never a
license to rewrite a stranger's repo.

``resolve_repo_mode`` is what skills/hooks consume. It honours an explicit
``repo_mode`` override (DB-home #1775 — resolved via
:func:`teatree.config.get_effective_settings`), else returns a 7-day-cached
detection (same cache shape as ``config.check_for_updates``) keyed by repo path.
"""

import hashlib
import json
import time
from enum import StrEnum
from pathlib import Path

from teatree.paths import DATA_DIR
from teatree.utils import git

_DEFAULT_SINCE_DAYS = 90
_DEFAULT_SOLO_THRESHOLD = 0.8
_CACHE_TTL_SECONDS = 7 * 86_400


class RepoMode(StrEnum):
    SOLO = "solo"
    COLLABORATIVE = "collaborative"

    @classmethod
    def parse(cls, value: str) -> "RepoMode":
        normalised = value.strip().lower()
        try:
            return cls(normalised)
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            msg = f"Invalid repo_mode {value!r}; valid values: {valid}"
            raise ValueError(msg) from exc


def _windowed_author_counts(repo: str, since_days: int) -> list[int]:
    branch = git.default_branch(repo)
    out = git.run(
        repo=repo,
        args=["shortlog", "-sn", "--no-merges", f"--since={since_days} days ago", branch],
    )
    counts: list[int] = []
    for line in out.splitlines():
        head = line.strip().split("\t", 1)[0].strip()
        if head.isdigit():
            counts.append(int(head))
    return counts


def detect_repo_mode(
    repo: str = ".",
    *,
    since_days: int = _DEFAULT_SINCE_DAYS,
    solo_threshold: float = _DEFAULT_SOLO_THRESHOLD,
) -> RepoMode:
    counts = _windowed_author_counts(repo, since_days)
    total = sum(counts)
    if total == 0:
        return RepoMode.COLLABORATIVE
    if max(counts) / total >= solo_threshold:
        return RepoMode.SOLO
    return RepoMode.COLLABORATIVE


def _config_override() -> RepoMode | None:
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: call-time import, kept lazy

    raw = get_effective_settings().repo_mode
    if not raw:
        return None
    return RepoMode.parse(raw)


def _cache_path(repo: str) -> Path:
    slug = hashlib.sha256(repo.encode()).hexdigest()[:12]
    return DATA_DIR / "repo-mode" / f"{slug}.json"


def _read_fresh_cache(repo: str) -> RepoMode | None:
    cache_path = _cache_path(repo)
    if not cache_path.is_file():
        return None
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if time.time() - cached.get("ts", 0) >= _CACHE_TTL_SECONDS:
        return None
    return RepoMode.parse(cached["mode"])


def _write_cache(repo: str, mode: RepoMode) -> None:
    cache_path = _cache_path(repo)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({"ts": time.time(), "mode": mode.value, "repo": repo}),
        encoding="utf-8",
    )


def resolve_repo_mode(repo: str = ".", *, refresh: bool = False) -> RepoMode:
    override = _config_override()
    if override is not None:
        return override

    if not refresh:
        cached = _read_fresh_cache(repo)
        if cached is not None:
            return cached

    mode = detect_repo_mode(repo)
    _write_cache(repo, mode)
    return mode
