"""A content fingerprint for deciding whether a checkout still has its attested state."""

import hashlib
import stat
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING

from teatree.core.models.ticket_worktree_checks import _resolve_base_branch
from teatree.core.worktree.target_branch import qualified_target, resolve_pr_target_branch
from teatree.utils import git
from teatree.utils.git_run import run_strict, run_strict_verbatim
from teatree.utils.run import CommandFailedError

if TYPE_CHECKING:
    from teatree.core.models import Ticket


@dataclass(frozen=True, slots=True)
class CheckoutState:
    digest: str
    commits_ahead: int
    uncommitted: int
    branch: str = ""

    @property
    def dirty(self) -> bool:
        return self.uncommitted > 0

    @property
    def pristine(self) -> bool:
        return self.commits_ahead == 0 and self.uncommitted == 0


def checkout_digest(
    head: str, branch: str, *, content: tuple[str, str] = ("", ""), untracked: tuple[Path, str] | None = None
) -> str:
    digest = hashlib.sha256()
    for field in (head, branch, *content):
        digest.update(field.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
    if untracked is not None:
        checkout, names = untracked
        for name in filter(None, names.split("\0")):
            path = checkout / name
            mode = path.lstat().st_mode
            digest.update(name.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
            if stat.S_ISREG(mode):
                digest.update(path.read_bytes())
            elif stat.S_ISLNK(mode):
                digest.update(str(path.readlink()).encode("utf-8", errors="surrogateescape"))
            else:
                digest.update(str(mode).encode())
            digest.update(b"\0")
    return digest.hexdigest()


def _base_ref(repo: str, ticket: "Ticket | None", branch: str) -> str:
    try:
        slug = git.remote_slug(repo=repo)
    except (CommandFailedError, RuntimeError, ValueError):
        slug = ""
    stacked = resolve_pr_target_branch(ticket, repo_slug=slug, branch=branch)
    return qualified_target(stacked) if stacked else _resolve_base_branch(repo)


def checkout_state(checkout: Path, *, ticket: "Ticket | None" = None) -> CheckoutState:
    repo = str(checkout)
    head = run_strict(repo=repo, args=["rev-parse", "--verify", "HEAD"])
    branch = run_strict(repo=repo, args=["rev-parse", "--abbrev-ref", "HEAD"])
    base = _base_ref(repo, ticket, branch)
    commits_ahead = int(run_strict(repo=repo, args=["rev-list", "--count", f"{base}..HEAD"]))
    status = run_strict_verbatim(repo=repo, args=["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    diff = run_strict_verbatim(repo=repo, args=["diff", "HEAD", "--binary", "--no-ext-diff"])
    untracked = run_strict_verbatim(repo=repo, args=["ls-files", "--others", "--exclude-standard", "-z"])
    digest = checkout_digest(head, branch, content=(status, diff), untracked=(checkout, untracked))
    return CheckoutState(digest, commits_ahead, _changed_paths(status), branch)


def _entry_paths(porcelain_z: str) -> list[list[str]]:
    fields = iter(filter(None, porcelain_z.split("\0")))
    entries = []
    for entry in fields:
        paths = [entry[3:]]
        if {"R", "C"} & set(entry[:2]):
            paths.extend(islice(fields, 1))  # -z writes a rename's source path as its own field
        entries.append(paths)
    return entries


def _changed_paths(porcelain_z: str) -> int:
    return len(_entry_paths(porcelain_z))


def uncommitted_paths(checkout: Path) -> list[Path]:
    status = run_strict_verbatim(repo=str(checkout), args=["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    return [checkout / path for paths in _entry_paths(status) for path in paths]
