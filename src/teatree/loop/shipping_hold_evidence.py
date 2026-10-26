"""Disk and forge evidence for settling a held no-diff review."""

import re
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from teatree.backends.loader import get_code_host_for_url
from teatree.config.loader import worktree_root
from teatree.core.models.ticket import Ticket
from teatree.core.models.ticket_worktree_checks import WorktreeProbeUnverifiableError, worktree_has_commits_ahead
from teatree.core.models.worktree import Worktree
from teatree.core.overlay_loader import get_overlay
from teatree.utils.forge import forge_from_remote
from teatree.utils.git_remote import host_from_remote, slug_from_remote
from teatree.utils.git_run import run_with_status
from teatree.utils.url_slug import slug_from_issue_or_pr_url


@dataclass(frozen=True)
class Evidence:
    found: str = ""
    unverified: str = ""


def _issue_project(ticket: Ticket) -> tuple[str, str, str]:
    url = ticket.issue_url
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    slug = slug_from_issue_or_pr_url(parsed.path) if parsed.scheme in {"http", "https"} else ""
    number = ticket.issue_number
    return slug, number, host


def _mentions(number: str, text: str) -> bool:
    return re.search(rf"(?<!\d){re.escape(number)}(?!\d)", text) is not None


def _checkout_paths(root: Path, number: str) -> list[Path]:
    try:
        mode = root.stat().st_mode
    except FileNotFoundError:
        return []
    if not stat.S_ISDIR(mode):
        raise NotADirectoryError(root)
    paths = []
    for first in root.iterdir():
        if not first.is_dir():
            continue
        if (first / ".git").exists():
            paths.append(first)
            continue
        try:
            paths.extend(child for child in first.iterdir() if child.is_dir() and (child / ".git").exists())
        except OSError:
            if _mentions(number, first.name):
                raise
    return paths


def _checkout_evidence(path: Path, *, named: bool, ticket: Ticket) -> Evidence:
    slug, number, host = _issue_project(ticket)
    recorded = str((ticket.extra or {}).get("branch") or "")
    read_branch = run_with_status(repo=str(path), args=["rev-parse", "--abbrev-ref", "HEAD"])
    if read_branch.returncode != 0:
        return (
            Evidence(unverified=f"git ({path.name}: {read_branch.stderr.strip()[:120]})")
            if named or recorded
            else Evidence()
        )
    branch = read_branch.stdout.strip()
    if not (named or (recorded and branch == recorded) or _mentions(number, branch)):
        return Evidence()
    read_remote = run_with_status(repo=str(path), args=["remote", "get-url", "origin"])
    if read_remote.returncode != 0:
        return Evidence(unverified=f"git ({path.name}: no readable origin remote)")
    remote = read_remote.stdout.strip()
    if slug_from_remote(remote).lower() != slug.lower() or host_from_remote(remote) != host:
        return Evidence()
    try:
        probe = Worktree(repo_path=slug, branch=branch, extra={"worktree_path": str(path)})
        return Evidence(found=f"checkout {path}") if worktree_has_commits_ahead(probe) else Evidence()
    except (WorktreeProbeUnverifiableError, OSError, ValueError, RuntimeError) as exc:
        return Evidence(unverified=f"git ({type(exc).__name__}: {exc})")


def disk_evidence(ticket: Ticket) -> Evidence:
    slug, number, _ = _issue_project(ticket)
    if not slug or not number:
        return Evidence(unverified="issue URL (no readable project or issue number)")
    try:
        root = worktree_root(overlay=ticket.overlay)
        paths = _checkout_paths(root, number)
    except OSError as exc:
        return Evidence(unverified=f"workspaces root ({type(exc).__name__}: {exc})")
    for path in paths:
        evidence = _checkout_evidence(path, named=_mentions(number, str(path.relative_to(root))), ticket=ticket)
        if evidence.found or evidence.unverified:
            return evidence
    return Evidence()


def forge_evidence(ticket: Ticket) -> Evidence:
    slug, number, _ = _issue_project(ticket)
    if not slug or not number or not forge_from_remote(ticket.issue_url):
        return Evidence(unverified="issue URL (no readable project or issue number)")
    try:
        host = get_code_host_for_url(get_overlay(ticket.overlay), ticket.issue_url)
        if host is None:
            return Evidence(unverified="forge (no readable host)")
        prefix = f"{number}-"
        branches = host.list_unshipped_branches(repo=slug, prefix=prefix)
        recorded = str((ticket.extra or {}).get("branch") or "")
        if recorded and not recorded.startswith(prefix):
            branches.extend(
                name for name in host.list_unshipped_branches(repo=slug, prefix=recorded) if name == recorded
            )
        if branches:
            return Evidence(found=f"branch {branches[0]}")
    except Exception as exc:  # noqa: BLE001 — every failed forge read is UNKNOWN, never empty
        return Evidence(unverified=f"forge ({type(exc).__name__}: {str(exc)[:160]})")
    return Evidence()
