"""Scan-local forge and origin reads shared by branch workers."""

import logging
from collections.abc import Generator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from importlib import import_module
from threading import Lock

from teatree.git_sha import is_full_git_sha
from teatree.utils.git_run import git_env_non_interactive, run_with_status
from teatree.utils.run import TimeoutExpired

logger = logging.getLogger(__name__)


class BranchScanIndex:
    def __init__(self) -> None:
        self._locks_guard = Lock()
        self._locks: dict[str, Lock] = {}
        self._open_prs: dict[str, dict[str, str] | None] = {}
        self._origin_heads: dict[str, set[str] | None] = {}

    def _lock(self, repo: str) -> Lock:
        with self._locks_guard:
            return self._locks.setdefault(repo, Lock())

    def open_pr_url(self, repo: str, branch: str) -> str | None:
        with self._lock(repo):
            if repo not in self._open_prs:
                self._open_prs[repo] = self._read_open_prs(repo)
            found = self._open_prs[repo]
        return None if found is None else found.get(branch, "")

    @staticmethod
    def _read_open_prs(repo: str) -> dict[str, str] | None:
        with suppress(Exception):
            host = import_module("teatree.core.backend_factory").code_host_for_repo_from_overlay(repo)
            if host is None or not isinstance(host.get_repo(repo=repo).get("id"), int):
                logger.warning("forge scan: could not resolve project for %s — unknown", repo)
                return None
            rows = host.list_prs(repo=repo, state="open")
            if not isinstance(rows, list):
                logger.warning("forge scan: open PR list for %s was incomplete — unknown", repo)
                return None
            found: dict[str, str] = {}
            for row in rows:
                if not isinstance(row, dict):
                    logger.warning("forge scan: open PR list for %s was malformed — unknown", repo)
                    return None
                branch = row.get("source_branch")
                url = row.get("web_url")
                if not isinstance(branch, str) or not branch or not isinstance(url, str) or not url:
                    logger.warning("forge scan: open PR list for %s was malformed — unknown", repo)
                    return None
                found[branch] = url
            return found
        logger.warning("forge scan: could not read complete open PR list for %s — unknown", repo)
        return None

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
