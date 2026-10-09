"""SessionEnd backstop — report every work-bearing state the session leaves behind.

Extracted whole from ``hook_router`` (the shrink-only dispatcher re-exports
:func:`handle_session_end` into ``_HANDLERS``) and widened along two axes:

**Armed unconditionally.** Whether a session stranded work is not a function of
which skills it loaded, so the sweep runs on every session end.

**All five work-bearing states**, not orphan branches alone: unstaged and staged
changes in the harness cwd, commits absent from every remote, a pushed branch with
no PR, and an open unmerged PR authored under this identity. Each item names itself
and the exact command that advances it.

Dirtiness is decided by ``git status --porcelain`` — index-aware. A bare
``git diff`` returns zero bytes against a worktree holding only staged work, which
is how 79 KB of staged changes read as CLEAN.

**Left for the next session in the checkout.** Nothing a SessionEnd hook prints reaches
a model, so the report is stored by :mod:`hooks.scripts.stranded_work_report` and the
next ``SessionStart`` in the same checkout delivers it (``stranded_work_start.py``); this
hook prints nothing and never fails.

Fail-OPEN and crash-proof: a probe that times out or finds no tooling contributes nothing,
and the handler as a whole swallows every error. A hook that throws blocks the session,
which is worse than a missed warning. Only an end whose probes ALL answered may clear the
earlier report; one where some could not look replaces the findings of the probes that
answered and keeps the earlier findings of the ones that could not. Each finding is dated
by its first sighting, so a kept one ages out on its own and never takes this end's fresh
findings down with it.

Cold-import safe: the module top imports only stdlib plus the already-extracted
``stop_snapshot_slot`` / ``t3_invocation`` siblings.
"""

import json
import re
import subprocess  # noqa: S404 — fixed-argv git probes, no shell; the `t3` probe is the seam's
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from hooks.scripts.stop_snapshot_slot import read_open_prs
from hooks.scripts.stranded_work_report import checkout_root, clear, found_at, found_line, leave, peek
from hooks.scripts.t3_invocation import run_t3, t3_argv

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# re-exports and a test patching a helper here operate on ONE module object.
sys.modules.setdefault("session_end_work_check", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.session_end_work_check", sys.modules[__name__])

PROBE_TIMEOUT_SECONDS = 4
PREVIEW_LIMIT = 5

_CLEAN_INDEX_CODES = frozenset({" ", "?"})
_PORCELAIN_PREFIX_WIDTH = 3


@dataclass(frozen=True, slots=True)
class WorkItem:
    """One stranded work-bearing state, with the command that advances it."""

    state: str
    label: str
    command: str
    #: When an end first found it; ``None`` for a finding of the end rendering it now.
    found_at: float | None = None


_ORPHAN_STATES = frozenset({"orphan_branch"})
_WORKTREE_STATES = frozenset({"staged", "unstaged", "unpushed"})
_PR_STATES = frozenset({"open_pr"})


@dataclass(frozen=True, slots=True)
class WorkScan:
    """Every stranded state found, and the states whose probe could not answer — only a complete scan may clear."""

    items: list[WorkItem]
    unanswered: frozenset[str] = frozenset()

    @property
    def complete(self) -> bool:
        return not self.unanswered


class ProbeFailedError(Exception):
    """A probe that could not look (timed out, no ``git``) — unlike one that looked and found nothing."""


#: The exit a quiet (``-q``) ref query gives for a ref that does not exist: git's answer, not an error.
_REF_ABSENT_EXIT = 1


def _git(repo: Path, *args: str, absent_is_empty: bool = False) -> str:
    """Raw stdout, trailing newlines only removed — a porcelain row's leading column is data.

    :class:`ProbeFailedError` when git could not run or could not read the checkout (a corrupt
    index, dubious ownership). *absent_is_empty* is for a quiet ref query — no commit yet, no
    upstream, a detached HEAD — whose "no such ref" exit is git's answer and reads as ``""``.
    """
    try:
        return subprocess.check_output(  # noqa: S603 — trusted internal subprocess; fixed argv, no shell
            ["git", "-C", str(repo), "--no-optional-locks", *args],  # noqa: S607 — trusted internal git invocation
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            stderr=subprocess.DEVNULL,
        ).rstrip("\n")
    except subprocess.CalledProcessError as exc:
        if absent_is_empty and exc.returncode == _REF_ABSENT_EXIT:
            return ""
        raise ProbeFailedError(str(exc)) from exc
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise ProbeFailedError(str(exc)) from exc


def _porcelain_rows(repo: Path) -> list[tuple[str, str, str]]:
    """``(index_code, worktree_code, path)`` per ``git status --porcelain`` row."""
    rows: list[tuple[str, str, str]] = []
    for line in _git(repo, "status", "--porcelain").splitlines():
        if len(line) < _PORCELAIN_PREFIX_WIDTH:
            continue
        rows.append((line[0], line[1], line[_PORCELAIN_PREFIX_WIDTH:].strip()))
    return rows


def staged_paths(repo: Path) -> list[str]:
    """Paths staged in the index — invisible to a bare ``git diff`` (defect B)."""
    return [path for index, _worktree, path in _porcelain_rows(repo) if index not in _CLEAN_INDEX_CODES]


def unstaged_paths(repo: Path) -> list[str]:
    """Paths modified or untracked in the working tree."""
    return [path for _index, worktree, path in _porcelain_rows(repo) if worktree != " "]


def unpushed_commit_count(repo: Path) -> int:
    """Commits on HEAD absent from the branch's upstream, or from every remote; none before the first commit."""
    if not _git(repo, "rev-parse", "--verify", "-q", "HEAD", absent_is_empty=True):
        return 0
    if _git(repo, "rev-parse", "--verify", "-q", "@{u}", absent_is_empty=True):
        return len(_git(repo, "log", "@{u}..HEAD", "--oneline").splitlines())
    return len(_git(repo, "log", "HEAD", "--not", "--remotes", "--oneline").splitlines())


def current_branch(repo: Path) -> str:
    """The checked-out branch, even before its first commit; ``(detached)`` when HEAD names no branch."""
    return _git(repo, "symbolic-ref", "--short", "-q", "HEAD", absent_is_empty=True).strip() or "(detached)"


def fetch_orphans() -> list[dict] | None:
    """``t3 teatree workspace list-orphans --json``, or ``None`` when it could not answer."""
    argv = t3_argv("teatree", "workspace", "list-orphans", "--json")
    if argv is None:
        return None
    try:
        result = run_t3(argv, timeout=PROBE_TIMEOUT_SECONDS)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


def _worktree_items(repo: Path) -> list[WorkItem]:
    branch = current_branch(repo)
    items: list[WorkItem] = []
    staged = staged_paths(repo)
    if staged:
        items.append(
            WorkItem(
                state="staged",
                label=f"{repo} ({branch}) — {len(staged)} staged, uncommitted file(s): {_names(staged)}",
                command=f"git -C {repo} commit",
            )
        )
    unstaged = unstaged_paths(repo)
    if unstaged:
        items.append(
            WorkItem(
                state="unstaged",
                label=f"{repo} ({branch}) — {len(unstaged)} unstaged/untracked file(s): {_names(unstaged)}",
                command=f"git -C {repo} add -A && git -C {repo} commit",
            )
        )
    unpushed = unpushed_commit_count(repo)
    if unpushed:
        items.append(
            WorkItem(
                state="unpushed",
                label=f"{repo} ({branch}) — {unpushed} commit(s) on no remote",
                command=f"git -C {repo} push -u origin {branch}",
            )
        )
    return items


def _names(paths: list[str]) -> str:
    preview = paths[:PREVIEW_LIMIT]
    suffix = f", +{len(paths) - len(preview)} more" if len(paths) > len(preview) else ""
    return ", ".join(preview) + suffix


def _orphan_command(repo: str, branch: str, status: str) -> str:
    ensure_pr = f"t3 teatree pr ensure-pr --repo {repo} --branch {branch}"
    if status == "pushed_orphan":
        return ensure_pr
    if status == "remote_unknown":
        return f"the remote of {repo} could not be read: check the network and credentials, then {ensure_pr}"
    return f"git -C {repo} push -u origin {branch} && {ensure_pr}"


def _orphan_items(orphans: list[dict]) -> list[WorkItem]:
    items: list[WorkItem] = []
    for orphan in orphans[:PREVIEW_LIMIT]:
        repo = orphan.get("repo", "?")
        branch = orphan.get("branch", "?")
        ahead = orphan.get("ahead_count", 0)
        command = _orphan_command(repo, branch, orphan.get("status", ""))
        items.append(
            WorkItem(
                state="orphan_branch",
                label=f"{repo} ({branch}) — {ahead} commit(s) ahead of main, no PR",
                command=command,
            )
        )
    return items


def _open_pr_items(prs: list[dict]) -> list[WorkItem]:
    return [
        WorkItem(
            state="open_pr",
            label=f"#{pr.get('number', '?')} {pr.get('title', '(no title)')} — open, not merged",
            command="t3 loops tick --loop ship",
        )
        for pr in prs[:PREVIEW_LIMIT]
    ]


def scan_work(cwd: str) -> WorkScan:
    """Every work-bearing state this session leaves behind in its checkout, best-effort."""
    items: list[WorkItem] = []
    unanswered: set[str] = set()
    orphans = fetch_orphans()
    if orphans is None:
        unanswered |= _ORPHAN_STATES
    else:
        items += _orphan_items(orphans)
    repo = checkout_root(cwd)
    if (repo / ".git").exists():
        try:
            items += _worktree_items(repo)
        except ProbeFailedError:
            unanswered |= _WORKTREE_STATES
        prs = read_open_prs(repo)
        if prs is None:
            unanswered |= _PR_STATES
        else:
            items += _open_pr_items(prs)
    return WorkScan(items, frozenset(unanswered))


def render_work_report(items: list[WorkItem]) -> str:
    """Every item with its next command and its first sighting — no count, as a start drops the aged-out ones."""
    header = (
        "UNSHIPPED WORK AT SESSION END — the ending session authored work that is "
        "neither merged nor tracked. No work-bearing state is terminal:"
    )
    now = time.time()
    lines = [header]
    for item in items:
        found = found_line(now if item.found_at is None else item.found_at)
        lines.extend((f"  - [{item.state}] {item.label}", f"      next: {item.command}", found))
    return "\n".join(lines)


#: One item as :func:`render_work_report` lays it out; a report an older end left may carry no ``found`` line.
_RENDERED_ITEM = re.compile(
    r"^  - \[(?P<state>\w+)\] (?P<label>.*)\n      next: (?P<command>.*)(?P<found>\n      found: \S+)?$", re.MULTILINE
)


def _carried(cwd: str, unanswered: frozenset[str]) -> list[WorkItem]:
    """The undelivered findings an earlier end left for the probes that could not answer now, each dated as it was."""
    earlier = peek(cwd) if unanswered else None
    if earlier is None:
        return []
    return [
        WorkItem(match["state"], match["label"], match["command"], found_at(match["found"] or "") or earlier.left_at)
        for match in _RENDERED_ITEM.finditer(earlier.text)
        if match["state"] in unanswered
    ]


def handle_session_end(data: dict) -> None:
    """Leave every stranded work-bearing state for the next session in this checkout; print nothing."""
    if data.get("session_id") and data.get("cwd"):
        _leave_report(str(data["cwd"]))


def _leave_report(cwd: str) -> None:
    try:
        scan = scan_work(cwd)
        if scan.complete and not scan.items:
            clear(cwd)
            return
        items = [*scan.items, *_carried(cwd, scan.unanswered)]
        leave(cwd, render_work_report(items) if items else "")
    except Exception:  # noqa: BLE001 — a session-end advisory must never break the session
        return
