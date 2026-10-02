"""Which of prek's leftover patches hold work that exists nowhere else (#144).

prek clears unstaged changes before running hooks, saves them to
``~/.cache/prek/patches/<epoch-ms>-<pid>.patch``, and restores them when the run
ends. Measured on prek 0.3.8: the restore runs on a normal exit and on SIGINT,
and does NOT run on SIGTERM or SIGKILL. So a run killed by ``timeout``, by
``docker stop``, by a supervisor deadline or by an OOM leaves the working tree
holding only the STAGED version, with the patch as the only surviving copy — and
the message naming that patch scrolled past inside the process that died.

A leftover patch is not itself a signal: prek keeps one after a SUCCESSFUL
restore too, and this host held 130 of them. Only the TREE separates the two, so
the question is asked of git rather than guessed from the file:

* applies in REVERSE cleanly -> its content is PRESENT -> litter
* applies FORWARD cleanly    -> its content is ABSENT  -> restorable loss
* neither                    -> another repo's patch, or the tree moved on -> UNKNOWN

Reverse is asked first because a patch that reverse-applies is by definition
already in the tree, so it can never be the loss this module looks for. UNKNOWN
never restores, and nothing here ever deletes: the patch is the last copy of
whatever it holds, and a directory listing cannot tell which one that is.
"""

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from teatree.utils.git_run import git_env_without_overrides, run_with_status

logger = logging.getLogger(__name__)

#: Where prek writes them. Overridable per call — never read from anywhere else.
DEFAULT_PATCH_DIR = Path.home() / ".cache" / "prek" / "patches"
PATCH_GLOB = "*.patch"


class PatchState(Enum):
    """What the tree says about a patch's content."""

    RESTORABLE = "restorable"
    ALREADY_PRESENT = "already_present"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PrekPatch:
    """One patch, classified against one repo, with the reason stated."""

    path: Path
    state: PatchState
    reason: str = ""

    def render(self) -> str:
        return f"  {self.state.value:<16} {self.path.name}  ({self.reason})"


def _applies(repo: Path, patch: Path, *, reverse: bool) -> bool:
    args = ["apply", "--check", *(["--reverse"] if reverse else []), str(patch)]
    return run_with_status(repo=str(repo), args=args, env=git_env_without_overrides()).returncode == 0


def classify_patch(repo: Path, patch: Path) -> PrekPatch:
    """Decide what ``patch`` means for ``repo`` — measured, never inferred from the name."""
    if not patch.is_file():
        return PrekPatch(patch, PatchState.UNKNOWN, "not a file")
    if patch.stat().st_size == 0:
        return PrekPatch(patch, PatchState.ALREADY_PRESENT, "empty — holds nothing")
    if _applies(repo, patch, reverse=True):
        return PrekPatch(patch, PatchState.ALREADY_PRESENT, "content is already in the tree")
    if _applies(repo, patch, reverse=False):
        return PrekPatch(patch, PatchState.RESTORABLE, "content is ABSENT from the tree")
    return PrekPatch(patch, PatchState.UNKNOWN, "applies neither way — another repo, or the tree moved on")


def _sort_key(patch: Path) -> tuple[int, str]:
    """Newest first. prek names a patch ``<epoch-ms>-<pid>``, so the prefix IS the clock."""
    head = patch.name.split("-", 1)[0]
    return (-int(head) if head.isdigit() else 0, patch.name)


def classify_patches(repo: Path, patch_dir: Path | None = None) -> list[PrekPatch]:
    """Every patch in ``patch_dir``, classified against ``repo``, newest first.

    A missing patch dir is an empty answer, not an error: a host that has never
    had prek stash anything has none, which is the same list.
    """
    directory = patch_dir or DEFAULT_PATCH_DIR
    if not directory.is_dir():
        return []
    return [classify_patch(repo, patch) for patch in sorted(directory.glob(PATCH_GLOB), key=_sort_key)]


@dataclass(frozen=True, slots=True)
class RestoreOutcome:
    """What one restore did, and why it refused when it did."""

    patch: Path
    into: Path
    detail: str
    ok: bool
    #: A dry run must not READ as a restore — the verdict word is the whole report.
    dry_run: bool = False

    def render(self) -> str:
        verdict = "REFUSED" if not self.ok else ("would restore" if self.dry_run else "restored")
        return f"{verdict} {self.patch.name} -> {self.into}\n  {self.detail}"


def restore_patch(repo: Path, patch: Path, *, dry_run: bool = False) -> RestoreOutcome:
    """Apply ``patch`` back into ``repo``'s working tree.

    ``git apply`` is all-or-nothing per invocation, so a refusal leaves the tree
    exactly as it was. The patch file is never removed — it stays the last copy
    until the operator is satisfied the work is back.
    """
    if not patch.is_file():
        return RestoreOutcome(patch, repo, "no such patch file", ok=False, dry_run=dry_run)
    args = ["apply", *(["--check"] if dry_run else []), str(patch)]
    result = run_with_status(repo=str(repo), args=args, env=git_env_without_overrides())
    if result.returncode == 0:
        detail = "applies cleanly (nothing written)" if dry_run else "applied to the working tree"
        return RestoreOutcome(patch, repo, detail, ok=True, dry_run=dry_run)
    reason = result.stderr.strip() or f"git apply exited {result.returncode}"
    return RestoreOutcome(patch, repo, f"nothing written — {reason}", ok=False, dry_run=dry_run)


def resolve_patch_dir(override: str = "") -> Path:
    return Path(override).expanduser() if override.strip() else DEFAULT_PATCH_DIR


def render_report(repo: Path, patch_dir: Path) -> str:
    """One line per patch, restorable first, with the count that decides whether to act.

    A report that led with the 130 benign leftovers would bury the one line that
    matters, so the restorable ones are named first and counted in the header.
    """
    patches = classify_patches(repo, patch_dir)
    ordered = sorted(patches, key=lambda p: (p.state is not PatchState.RESTORABLE, p.path.name))
    restorable = [p for p in ordered if p.state is PatchState.RESTORABLE]
    header = f"prek patches in {patch_dir} against {repo}: {len(restorable)} restorable of {len(patches)}"
    if not patches:
        return f"{header}\n  none — nothing has been stashed on this host."
    tail = (
        [f"  restore one with: t3 <overlay> workspace prek-patches --repo {repo} --restore {restorable[0].path.name}"]
        if restorable
        else []
    )
    return "\n".join([header, *(p.render() for p in ordered), *tail])
