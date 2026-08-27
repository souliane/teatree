"""SessionEnd backstop — report every work-bearing state the session leaves behind.

Extracted whole from ``hook_router`` (the shrink-only dispatcher re-exports
:func:`handle_session_end` into ``_HANDLERS``) and widened along two axes:

**Armed unconditionally.** Whether a session stranded work is not a function of
which skills it loaded, so the sweep runs on every session end. The retro
suggestion keeps its own lifecycle-skill trigger.

**All five work-bearing states**, not orphan branches alone: unstaged and staged
changes in the harness cwd, commits absent from every remote, a pushed branch with
no PR, and an open unmerged PR authored under this identity. Each item names itself
and the exact command that advances it.

Dirtiness is decided by ``git status --porcelain`` — index-aware. A bare
``git diff`` returns zero bytes against a worktree holding only staged work, which
is how 79 KB of staged changes read as CLEAN.

Fail-OPEN and crash-proof: a probe that raises, times out, or finds no tooling
contributes nothing, and the handler as a whole swallows every error. A hook that
throws blocks the session, which is worse than a missed warning.

Cold-import safe: the module top imports only stdlib plus the already-extracted
``stop_snapshot_slot`` / ``t3_invocation`` siblings.
"""

import json
import subprocess  # noqa: S404 — fixed-argv git probes, no shell; the `t3` probe is the seam's
import sys
from dataclasses import dataclass
from pathlib import Path

from hooks.scripts.stop_snapshot_slot import open_prs_for_repo
from hooks.scripts.t3_invocation import run_t3, t3_argv

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# re-exports and a test patching a helper here operate on ONE module object.
sys.modules.setdefault("session_end_work_check", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.session_end_work_check", sys.modules[__name__])

PROBE_TIMEOUT_SECONDS = 4
PREVIEW_LIMIT = 5

LIFECYCLE_SKILLS = frozenset({"t3:code", "t3:debug", "t3:test", "t3:ship", "t3:review", "t3:ticket"})

_CLEAN_INDEX_CODES = frozenset({" ", "?"})
_PORCELAIN_PREFIX_WIDTH = 3


@dataclass(frozen=True, slots=True)
class WorkItem:
    """One stranded work-bearing state, with the command that advances it."""

    state: str
    label: str
    command: str


def _git(repo: Path, *args: str) -> str:
    """Raw stdout, trailing newlines only removed — a porcelain row's leading column is data."""
    try:
        return subprocess.check_output(  # noqa: S603 — trusted internal subprocess; fixed argv, no shell
            ["git", "-C", str(repo), "--no-optional-locks", *args],  # noqa: S607 — trusted internal git invocation
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            stderr=subprocess.DEVNULL,
        ).rstrip("\n")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


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
    """Commits on HEAD absent from the branch's upstream, or from every remote."""
    against_upstream = _git(repo, "log", "@{u}..HEAD", "--oneline")
    if against_upstream:
        return len(against_upstream.splitlines())
    if _git(repo, "rev-parse", "--abbrev-ref", "@{u}"):
        return 0
    return len(_git(repo, "log", "HEAD", "--not", "--remotes", "--oneline").splitlines())


def current_branch(repo: Path) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() or "(detached)"


def fetch_orphans() -> list[dict]:
    """``t3 teatree workspace list-orphans`` as JSON, or ``[]`` on any failure."""
    argv = t3_argv("teatree", "workspace", "list-orphans")
    if argv is None:
        return []
    try:
        result = run_t3(argv, timeout=PROBE_TIMEOUT_SECONDS)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


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


def _orphan_items() -> list[WorkItem]:
    items: list[WorkItem] = []
    for orphan in fetch_orphans()[:PREVIEW_LIMIT]:
        repo = orphan.get("repo", "?")
        branch = orphan.get("branch", "?")
        ahead = orphan.get("ahead_count", 0)
        pushed = orphan.get("status", "") == "pushed_orphan"
        command = (
            f"t3 teatree pr ensure-pr --branch {branch}"
            if pushed
            else f"git -C {repo} push -u origin {branch} && t3 teatree pr ensure-pr --branch {branch}"
        )
        items.append(
            WorkItem(
                state="orphan_branch",
                label=f"{repo} ({branch}) — {ahead} commit(s) ahead of main, no PR",
                command=command,
            )
        )
    return items


def _open_pr_items(repo: Path) -> list[WorkItem]:
    return [
        WorkItem(
            state="open_pr",
            label=f"#{pr.get('number', '?')} {pr.get('title', '(no title)')} — open, not merged",
            command="t3 loops tick --loop ship",
        )
        for pr in open_prs_for_repo(repo)[:PREVIEW_LIMIT]
    ]


def collect_work_items(cwd: str) -> list[WorkItem]:
    """Every work-bearing state this session leaves behind, best-effort."""
    items = _orphan_items()
    repo = Path(cwd) if cwd else None
    if repo is not None and repo.is_dir() and (repo / ".git").exists():
        items += _worktree_items(repo)
        items += _open_pr_items(repo)
    return items


def render_work_report(items: list[WorkItem]) -> str:
    header = (
        f"UNSHIPPED WORK AT SESSION END ({len(items)}) — this session authored work that is "
        "neither merged nor tracked. No work-bearing state is terminal:"
    )
    lines = [header]
    for item in items:
        lines.extend((f"  - [{item.state}] {item.label}", f"      next: {item.command}"))
    return "\n".join(lines)


def _loaded_skills(session_id: str, state_dir: Path) -> set[str]:
    skills_file = state_dir / f"{session_id}.skills"
    if not skills_file.is_file():
        return set()
    return {line.strip() for line in skills_file.read_text(encoding="utf-8").splitlines() if line.strip()}


def _retro_part(loaded: set[str]) -> str:
    lifecycle = loaded & LIFECYCLE_SKILLS
    if not lifecycle:
        return ""
    return (
        f"SESSION ENDING — lifecycle skills were loaded during this session ({', '.join(sorted(lifecycle))}). "
        "Consider running /t3:retro to capture learnings before the session ends."
    )


def handle_session_end(data: dict) -> None:
    """Suggest retro and surface every stranded work-bearing state at session close."""
    session_id = data.get("session_id", "")
    if not session_id:
        return
    try:
        from hooks.scripts.hook_router import STATE_DIR  # noqa: PLC0415 — deferred: avoids an import cycle

        parts = [_retro_part(_loaded_skills(session_id, STATE_DIR))]
        items = collect_work_items(data.get("cwd", "") or "")
        if items:
            parts.append(render_work_report(items))
    except Exception:  # noqa: BLE001 — a session-end advisory must never break the session
        return
    populated = [part for part in parts if part]
    if not populated:
        return
    json.dump({"additionalContext": "\n\n".join(populated)}, sys.stdout)
