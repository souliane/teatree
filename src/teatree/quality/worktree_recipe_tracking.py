"""Fitness function: a published ``git worktree add`` recipe never leaves the branch tracking.

``git worktree add -b <branch> <path> origin/main`` makes git configure the NEW
branch to track ``origin/main``, so ``branch.<branch>.merge`` reads
``refs/heads/main`` (#4225). Under ``push.default = upstream`` a routine
``git push`` on that branch then aims straight at ``main``, with forge-side
branch protection the only remaining barrier; under git's unchosen ``simple``
default the same config instead makes the push refuse over the mismatched names.
``utils/git_upstream.py`` repairs branches that already drifted and
``utils/git_worktree.worktree_add`` normalises the ones teatree creates — but a
recipe an agent copies out of a skill bypasses both, so the published text is its
own surface and needs its own guard.

The defect is a SHAPE, not a string: an invocation that CREATES a branch
(``-b``/``-B``) from a REMOTE start point (``origin/<branch>``) and omits
``--no-track``. Recipes that create no branch, or start from a local ref, cannot
reach it and are never flagged.

Detection is textual and stdlib-only. Each markdown line is reduced to its code
FRAGMENTS — the whole line inside a fence, the backticked spans outside one — so
a prose sentence quoting two commands cannot lend one command's flags to the
other, and each ``git worktree add`` is then cut at the first shell separator or
comment so a trailing ``# FORBIDDEN`` is not read as argv. Backslash
continuations are joined first, so a ``--no-track`` on the recipe's second line
still counts.

Exempt: a line carrying ``worktree-tracking: allow`` — the escape for prose that
quotes the defective shape BECAUSE it is the defect being described.
"""

import dataclasses
import re
import shlex
import sys
from pathlib import Path

from teatree.utils.disposable_checkout import disposable_roots

ALLOW_PRAGMA = "worktree-tracking: allow"
PLACEMENT_ALLOW_PRAGMA = "worktree-placement: allow"
NO_TRACK = "--no-track"

_SCANNED_ROOTS: tuple[str, ...] = ("skills", "docs")
_FENCE_RE = re.compile(r"^\s*```")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_WORKTREE_ADD_RE = re.compile(r"git\s+worktree\s+add\b")
_WORKTREE_CREATE_RE = re.compile(r"git\s+worktree\s+(add|move)\b")
_TERMINATOR_RE = re.compile(r"&&|\|\||;|\||#")
_BRANCH_FLAG_RE = re.compile(r"(?<![\w-])-[bB](?![\w-])")
_REMOTE_START_POINT_RE = re.compile(r"(?<![\w./-])(?:origin|upstream|<remote>)/[A-Za-z0-9._<>/-]+")


@dataclasses.dataclass(frozen=True)
class Finding:
    path: str
    line_no: int
    argv: str

    @property
    def message(self) -> str:
        return (
            f"{self.path}:{self.line_no}: `git worktree add{self.argv}` creates a branch from a remote "
            f"start point without `{NO_TRACK}` — the new branch then tracks that ref, so a routine "
            f"`git push` on it aims at the remote's branch (#4225). Add `{NO_TRACK}`, or mark the line "
            f"`{ALLOW_PRAGMA}` when quoting the defective shape IS the point."
        )


@dataclasses.dataclass(frozen=True)
class PlacementFinding:
    path: str
    line_no: int
    target: str

    @property
    def message(self) -> str:
        return (
            f"{self.path}:{self.line_no}: worktree target {self.target!r} is outside the allowed "
            "checkout roots. Use `~/workspace/t3-workspaces/`, `/tmp/`, or `/var/tmp/`; "
            f"mark an intentional quoted example `{PLACEMENT_ALLOW_PRAGMA}`."
        )


def logical_lines(source: str) -> list[tuple[int, str, bool]]:
    """``(first line number, joined text, inside a fence)`` per backslash-joined logical line."""
    out: list[tuple[int, str, bool]] = []
    in_fence = False
    pending: list[str] = []
    first = 0
    for line_no, raw in enumerate(source.splitlines(), 1):
        if _FENCE_RE.match(raw):
            in_fence = not in_fence
            continue
        stripped = raw.rstrip()
        if not pending:
            first = line_no
        if stripped.endswith("\\"):
            pending.append(stripped[:-1])
            continue
        pending.append(stripped)
        out.append((first, " ".join(part.strip() for part in pending), in_fence))
        pending = []
    if pending:
        out.append((first, " ".join(part.strip() for part in pending), in_fence))
    return out


def code_fragments(text: str, *, in_fence: bool) -> list[str]:
    if in_fence:
        return [text]
    return _INLINE_CODE_RE.findall(text)


def invocations_in(fragment: str) -> list[str]:
    """The argv tail of each ``git worktree add`` in ``fragment``, cut at the first separator."""
    found: list[str] = []
    for match in _WORKTREE_ADD_RE.finditer(fragment):
        tail = fragment[match.end() :]
        cut = _TERMINATOR_RE.search(tail)
        found.append(tail[: cut.start()] if cut else tail)
    return found


def is_defective(argv: str) -> bool:
    if NO_TRACK in argv:
        return False
    return bool(_BRANCH_FLAG_RE.search(argv) and _REMOTE_START_POINT_RE.search(argv))


def scan_source(source: str, path: str) -> list[Finding]:
    findings: list[Finding] = []
    for line_no, text, in_fence in logical_lines(source):
        if ALLOW_PRAGMA in text:
            continue
        for fragment in code_fragments(text, in_fence=in_fence):
            findings.extend(
                Finding(path=path, line_no=line_no, argv=argv.rstrip())
                for argv in invocations_in(fragment)
                if is_defective(argv)
            )
    return findings


def _recipe_target(verb: str, argv: str) -> str | None:
    try:
        words = shlex.split(argv)
    except ValueError:
        return None
    values: list[str] = []
    i = 0
    while i < len(words):
        word = words[i]
        if word in {"-b", "-B", "--orphan", "--reason"}:
            i += 2
            continue
        if not word.startswith("-"):
            values.append(word)
        i += 1
    if verb == "add":
        return values[0] if values else None
    return values[1] if len(values) > 1 else None


def _allowed_recipe_target(target: str) -> bool:
    if target.startswith(("~/", "$HOME/")):
        return target.startswith(("~/workspace/t3-workspaces/", "$HOME/workspace/t3-workspaces/"))
    if not target.startswith(("/", "~/")):
        return False
    location = Path(target).expanduser().resolve()
    roots = (Path.home() / "workspace" / "t3-workspaces", *disposable_roots())
    return any(location.is_relative_to(root.resolve()) for root in roots)


def scan_placement_source(source: str, path: str) -> list[PlacementFinding]:
    findings: list[PlacementFinding] = []
    for line_no, text, in_fence in logical_lines(source):
        if PLACEMENT_ALLOW_PRAGMA in text:
            continue
        for fragment in code_fragments(text, in_fence=in_fence):
            for match in _WORKTREE_CREATE_RE.finditer(fragment):
                prefix = fragment[: match.start()].rstrip()
                if prefix and not prefix.endswith(("&&", "||", ";", "|")):
                    continue
                tail = fragment[match.end() :]
                cut = _TERMINATOR_RE.search(tail)
                target = _recipe_target(match[1], tail[: cut.start()] if cut else tail)
                if target is not None and not _allowed_recipe_target(target):
                    findings.append(PlacementFinding(path, line_no, target))
    return findings


def collect_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for name in _SCANNED_ROOTS:
        scanned = root / name
        if not scanned.is_dir():
            continue
        files.extend(p for p in scanned.rglob("*.md") if p.is_file())
    return sorted(files)


def scan_tree(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in collect_files(root):
        rel = path.relative_to(root).as_posix()
        findings.extend(scan_source(path.read_text(encoding="utf-8"), rel))
    return findings


def scan_placement_tree(root: Path) -> list[PlacementFinding]:
    findings: list[PlacementFinding] = []
    for path in collect_files(root):
        rel = path.relative_to(root).as_posix()
        findings.extend(scan_placement_source(path.read_text(encoding="utf-8"), rel))
    return findings


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def run(root: Path) -> int:
    findings = [*scan_tree(root), *scan_placement_tree(root)]
    if not findings:
        sys.stdout.write("worktree-recipe-tracking: OK (tracking and placement recipes are safe)\n")
        return 0
    for finding in findings:
        sys.stdout.write(f"  - {finding.message}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(run(_repo_root()))
