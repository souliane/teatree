"""Scan-local origin reads shared by branch workers."""

import logging
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock

from teatree.git_sha import is_full_git_sha
from teatree.utils.git_run import git_env_non_interactive, run_with_status
from teatree.utils.run import TimeoutExpired

logger = logging.getLogger(__name__)


class BranchScanIndex:
    def __init__(self) -> None:
        self._locks_guard = Lock()
        self._locks: dict[str, Lock] = {}
        self._origin_heads: dict[str, set[str] | None] = {}

    def _lock(self, repo: str) -> Lock:
        with self._locks_guard:
            return self._locks.setdefault(repo, Lock())

    def origin_has_branch(self, repo: str, branch: str, *, timeout: float) -> bool | None:
        with self._lock(repo):
            if repo not in self._origin_heads:
                self._origin_heads[repo] = self._read_origin_heads(repo, timeout=timeout)
            heads = self._origin_heads[repo]
        return None if heads is None else branch in heads

    @staticmethod
    def _read_origin_heads(repo: str, *, timeout: float) -> set[str] | None:
        try:
            result = run_with_status(
                repo=repo,
                args=["ls-remote", "--heads", "origin"],
                env=git_env_non_interactive(),
                timeout=timeout,
            )
            if result.returncode != 0:
                logger.warning("forge scan: could not read origin heads for %s — remote_unknown", repo)
                return None
            heads: set[str] = set()
            for line in result.stdout.splitlines():
                sha, sep, ref = line.partition("\t")
                if not sep or not is_full_git_sha(sha) or not ref.startswith("refs/heads/"):
                    logger.warning("forge scan: malformed origin heads for %s — remote_unknown", repo)
                    return None
                heads.add(ref.removeprefix("refs/heads/"))
        except (TimeoutExpired, ValueError, OSError):
            logger.warning("forge scan: could not read origin heads for %s — remote_unknown", repo)
            return None
        else:
            return heads


_ACTIVE_INDEX: ContextVar[BranchScanIndex | None] = ContextVar("branch_scan_index", default=None)


def active_branch_scan_index() -> BranchScanIndex | None:
    return _ACTIVE_INDEX.get()


@contextmanager
def branch_scan_scope() -> Generator[BranchScanIndex]:
    existing = _ACTIVE_INDEX.get()
    if existing is not None:
        yield existing
        return
    index = BranchScanIndex()
    token = _ACTIVE_INDEX.set(index)
    try:
        yield index
    finally:
        _ACTIVE_INDEX.reset(token)
