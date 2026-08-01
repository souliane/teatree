"""A pre-commit stash whose restore FAILED leaves the tree clean — surface it.

``prek``/``pre-commit`` stash every unstaged change in the whole checkout before
running hooks and restore them afterwards, writing the stash to
``$PREK_HOME/patches/<epoch_ms>-<pid>.patch``. The restore can FAIL — the tree
moved on between the stash and the restore, so ``git apply`` reports ``patch does
not apply`` — and the working tree is then left holding NONE of those changes.
The author's edits are gone from disk; ``git status`` reads clean; the only
notice is one line in the middle of a hook log nobody re-reads.

Two facts decide the shape of this check, both measured rather than assumed.

The patch file is written on EVERY commit that has unstaged changes and is NEVER
deleted, not even after a restore that fully succeeded. So the file merely
EXISTING says nothing — a directory of them is the normal steady state, not a
pile of failures. The only signal left is therefore CONTENT: after a successful
restore the stashed additions are back in the working tree, and after a failed
one they are in no file at all.

Hence the test here — a file-hunk whose every added line is absent from the
working-tree copy of that same file. Requiring EVERY line to be absent is what
keeps this quiet: a lane that simply revised its own draft before committing
leaves most of the block intact and never matches, while a dropped restore
leaves the whole block missing.

It is bounded to a recent window on purpose. Deliberately abandoned edits are
indistinguishable from lost ones once they age — both are simply content that
exists in a patch and nowhere else — so an unbounded scan reports every edit
anyone ever thought better of. Within a day the reading is unambiguous enough to
act on, which is the only window in which acting is still cheap.

The root cause is not fixable here. The stash/restore cycle is unconditional in
prek (there is no opt-out flag), and the race that breaks it is two lanes
committing in ONE checkout — the restore's ``git apply`` runs against a tree
another lane has since moved. One checkout per lane removes it; nothing inside a
shared checkout does. So this check does not pretend to prevent the loss, only to
refuse to let it pass silently, and it names the patch that still holds the work.
"""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer

_DIFF_HEADER = re.compile(r"^diff --git (?:a/)?(\S+) (?:b/)?(\S+)$")
_RECENT = timedelta(hours=24)
_LISTED = 10
_PATHS_PER_LINE = 4
# A stash of a whole checkout can be tens of MB; reading every one of them on a
# doctor run costs more than the finding is worth.
_MAX_PATCH_BYTES = 4_000_000


def check_stranded_prek_patches() -> bool:
    """WARN once per recent pre-commit stash whose content reached no file.

    Advisory, never a gate — it reports work an operator has to look at, not an
    invariant teatree broke, and a red check no command can clear is the one its
    reader learns to scroll past. Always returns ``True``.
    """
    try:
        stranded = _stranded_patches(_patch_dir(), _repo_root())
    except Exception as exc:  # noqa: BLE001 — a doctor check must never crash the run
        typer.echo(
            f"WARN  Stranded pre-commit stashes UNVERIFIED: the patch cache could not be read "
            f"({exc.__class__.__name__}: {exc})."
        )
        return True
    if not stranded:
        return True
    typer.echo(
        f"WARN  {len(stranded)} pre-commit stash(es) from the last 24h hold changes that reached NO file in this "
        "checkout — the likely signature of a restore that failed and left the tree clean. Read one with "
        "`git apply --stat <patch>`, recover it with `git apply <patch>`. Verify before assuming loss: an edit "
        "its author deliberately dropped looks identical."
    )
    for patch, paths in stranded[:_LISTED]:
        shown = ", ".join(paths[:_PATHS_PER_LINE])
        typer.echo(f"      {patch} — {shown}{' …' if len(paths) > _PATHS_PER_LINE else ''}")
    if len(stranded) > _LISTED:
        typer.echo(f"      … and {len(stranded) - _LISTED} more in {_patch_dir()}.")
    return True


def _patch_dir() -> Path:
    """Where prek saves a stash. ``PREK_HOME`` wins, else prek's own default."""
    import os  # noqa: PLC0415 — deferred: keeps CLI startup light

    home = os.environ.get("PREK_HOME")
    return (Path(home) if home else Path.home() / ".cache" / "prek") / "patches"


def _repo_root() -> Path | None:
    """This checkout's root, or ``None`` when the cwd is not in one."""
    from teatree.utils.git_run import run  # noqa: PLC0415 — deferred: keeps CLI startup light

    top = run(repo=str(Path.cwd()), args=["rev-parse", "--show-toplevel"])
    return Path(top) if top else None


def _stranded_patches(patch_dir: Path, repo_root: Path | None) -> list[tuple[Path, list[str]]]:
    """Recent patches paired with the repo paths whose additions reached no file."""
    if repo_root is None or not patch_dir.is_dir():
        return []
    cutoff = (datetime.now(UTC) - _RECENT).timestamp()
    found: list[tuple[Path, list[str]]] = []
    for patch in sorted(patch_dir.glob("*.patch"), key=lambda p: p.stat().st_mtime, reverse=True):
        stat = patch.stat()
        if stat.st_mtime < cutoff or stat.st_size > _MAX_PATCH_BYTES:
            continue
        absent = _paths_absent_from_tree(patch.read_text(errors="replace"), repo_root)
        if absent:
            found.append((patch, absent))
    return found


def _paths_absent_from_tree(patch_text: str, repo_root: Path) -> list[str]:
    """Repo paths in *patch_text* none of whose added lines survive in the tree.

    A path the patch names that does not resolve inside THIS checkout is skipped
    rather than reported: the patch cache is one directory shared by every repo
    on the machine, so most entries belong to somebody else's checkout.
    """
    absent = []
    for path, added in _additions_by_path(patch_text).items():
        target = repo_root / path
        if not added or not target.is_file():
            continue
        try:
            body = target.read_text(errors="replace")
        except OSError:
            continue
        if all(line not in body for line in added):
            absent.append(path)
    return absent


def _additions_by_path(patch_text: str) -> dict[str, list[str]]:
    """Each path in the patch mapped to its non-blank added lines."""
    by_path: dict[str, list[str]] = {}
    current = ""
    for line in patch_text.splitlines():
        header = _DIFF_HEADER.match(line)
        if header:
            current = header.group(2)
            by_path.setdefault(current, [])
        elif current and line.startswith("+") and not line.startswith("+++") and line[1:].strip():
            by_path[current].append(line[1:])
    return by_path


__all__ = ["check_stranded_prek_patches"]
