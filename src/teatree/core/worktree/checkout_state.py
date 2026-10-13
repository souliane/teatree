"""A content fingerprint for deciding whether a checkout still has its attested state."""

import hashlib
import stat
from dataclasses import dataclass
from pathlib import Path

from teatree.core.models.ticket_worktree_checks import _resolve_base_branch
from teatree.utils.git_run import run_strict, run_strict_verbatim


@dataclass(frozen=True, slots=True)
class CheckoutState:
    digest: str
    commits_ahead: int
    dirty: bool

    @property
    def pristine(self) -> bool:
        return self.commits_ahead == 0 and not self.dirty


def checkout_state(checkout: Path) -> CheckoutState:
    repo = str(checkout)
    head = run_strict(repo=repo, args=["rev-parse", "--verify", "HEAD"])
    branch = run_strict(repo=repo, args=["symbolic-ref", "--quiet", "--short", "HEAD"])
    base = _resolve_base_branch(repo)
    commits_ahead = int(run_strict(repo=repo, args=["rev-list", "--count", f"{base}..HEAD"]))
    status = run_strict_verbatim(repo=repo, args=["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    diff = run_strict_verbatim(repo=repo, args=["diff", "HEAD", "--binary", "--no-ext-diff"])
    untracked = run_strict_verbatim(repo=repo, args=["ls-files", "--others", "--exclude-standard", "-z"])
    digest = hashlib.sha256()
    for field in (head, branch, status, diff):
        digest.update(field.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
    for name in filter(None, untracked.split("\0")):
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
    return CheckoutState(digest.hexdigest(), commits_ahead, bool(status))
