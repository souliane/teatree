"""Continuous stop-snapshotter — the shared implementation (souliane/teatree#2564, PR-20).

One idempotent operation, :func:`prepare_stop`, refreshes three durable
recovery artifacts so a session can be reconstructed after a ``/tmp`` wipe,
context compaction, or an unplanned stop:

- a mirror of the harness TODO list (:func:`write_todo_mirror`);
- a resume-plan file — open PRs, pending deferred questions, and the resolved
    availability/schedule state (:func:`write_resume_plan`);
- at-risk-worktree recovery (:func:`handle_at_risk_worktree`) — a worktree
    under ``/tmp`` or on an unpushed branch with uncommitted changes has its
    full working state (untracked files included) captured as a referenced
    commit object under ``refs/t3-resume/<slug>`` in the *shared* ``.git``,
    which lives outside the volatile working dir so the objects survive it.

The same function is called from three places (all thin adapters): the
always-on 5-minute Stop-hook slot, the ``PreCompact`` compaction event, and the
``t3 <overlay> session prepare-stop`` CLI. Artifacts are single deterministic
files / a single per-worktree ref, so re-running never duplicates work.

Recovery is git-object-native — a commit under ``refs/t3-resume/``, never a
serialized ``.patch`` / ``.bundle`` file (the teatree recovery invariant pinned
by ``tests/quality/test_no_git_work_to_file_serialization.py``). The capture
never touches the worktree's real index or branch history: it stages into a
throwaway ``GIT_INDEX_FILE`` and records the snapshot via git plumbing, so the
user's staging and branch are left exactly as they were.
"""

import logging
import os
import re
import subprocess  # noqa: S404 — only TimeoutExpired referenced; shell-outs go through teatree.utils.run
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from teatree.core import availability, harness_todos
from teatree.core.models import PullRequest
from teatree.core.session_identity import current_session_id
from teatree.utils.run import run_allowed_to_fail

logger = logging.getLogger(__name__)

_RESUME_REF_PREFIX = "refs/t3-resume/"
_GIT_TIMEOUT = 15


@dataclass(frozen=True, slots=True)
class AtRiskWorktree:
    """One at-risk worktree whose working state was captured for recovery."""

    path: Path
    branch: str
    recovery_ref: str
    recovery_commit: str
    uncommitted: int


@dataclass(frozen=True, slots=True)
class StopSnapshotResult:
    """The artifacts :func:`prepare_stop` refreshed this run."""

    session_id: str
    todos_path: Path | None
    resume_plan_path: Path | None
    at_risk: list[AtRiskWorktree] = field(default_factory=list)


def resume_dir(base: Path | None = None) -> Path:
    """The durable dir the recovery artifacts live in.

    ``base`` overrides for tests. The default is ``$XDG_STATE_HOME/teatree/
    resume`` (falling back to ``~/.local/state``) — XDG *state*, mirroring
    :func:`teatree.config.setting_parsers._default_handover_mirror_path`, because
    a resume snapshot is regenerable transient state, not durable user data. It
    is deliberately NOT the harness memory dir (``~/.claude/.../memory``): the
    dream/recall memory audit globs that dir, so a resume file there would
    pollute the lesson corpus.
    """
    if base is not None:
        return base
    xdg_state = os.environ.get("XDG_STATE_HOME")
    root = Path(xdg_state) if xdg_state else Path.home() / ".local" / "state"
    return root / "teatree" / "resume"


def _slug(value: str) -> str:
    """A filename- and ref-safe slug: ``[A-Za-z0-9._-]`` runs, collapsed."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return cleaned or "unknown"


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    """Best-effort ``git -C repo``; ``""`` on any failure (never raises).

    Routes through the typed :func:`teatree.utils.run.run_allowed_to_fail`
    egress wrapper (``expected_codes=None`` — any exit code is fine for a
    best-effort probe); a timeout / missing-binary degrades to ``""``.
    """
    try:
        result = run_allowed_to_fail(
            ["git", "-C", str(repo), "--no-optional-locks", *args],
            expected_codes=None,
            env=env,
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return result.stdout.strip()


def write_todo_mirror(session_id: str, *, base: Path | None = None) -> Path | None:
    """Mirror the harness TODO list to a durable per-session file.

    Reads the harness's own on-disk TODO store via
    :func:`teatree.core.harness_todos.read_harness_todos`; the file is written
    even when the list is empty so the artifact always exists and its freshness
    is measurable. Returns the path, or ``None`` when the session id is blank.
    """
    if not session_id:
        return None
    todos = harness_todos.read_harness_todos(session_id)
    lines = [f"# TODO mirror — session `{session_id}`", f"# refreshed {datetime.now(tz=UTC).isoformat()}", ""]
    lines += [f"- [{status}] {text}" for status, text in todos] or ["(no pending TODOs)"]
    target = resume_dir(base) / f"todos-{_slug(session_id)}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def _open_prs_lines() -> list[str]:
    prs = PullRequest.objects.exclude(state=PullRequest.State.MERGED).order_by("repo", "iid")
    rows = [f"- {pr.repo} #{pr.iid} ({pr.get_state_display()}) — {pr.url}" for pr in prs]
    return ["## Open PRs awaiting action", *(rows or ["(none)"])]


def _pending_questions_lines() -> list[str]:
    rows = [
        f"- {q.question.splitlines()[0] if q.question else '(empty)'}" for q in availability.iter_pending_questions()
    ]
    return ["## Pending deferred questions", *(rows or ["(none)"])]


def _availability_lines() -> list[str]:
    from teatree.loop.mode_resolution import resolve_active_mode  # noqa: PLC0415 — deferred: call-time import
    from teatree.loops.preset_status import schedule_chunk  # noqa: PLC0415 — deferred: call-time import

    resolved = resolve_active_mode()
    return [
        "## Mode / schedule",
        f"- mode: {resolved.name} (source: {resolved.source})",
        f"- defers questions: {resolved.defers_questions}",
        f"- pauses self-pump: {resolved.pauses_self_pump}",
        f"- {schedule_chunk()}",
    ]


def write_resume_plan(session_id: str, cwd: str, *, base: Path | None = None) -> Path:
    """Write the resume-plan file (open PRs, pending questions, availability).

    A single deterministic per-session file, overwritten each run. ``cwd`` is
    recorded so a recovering session knows where the work was happening.
    """
    lines = [
        f"# Resume plan — session `{session_id or '(unknown)'}`",
        f"refreshed {datetime.now(tz=UTC).isoformat()}",
        f"cwd: `{cwd}`",
        "",
        *_open_prs_lines(),
        "",
        *_pending_questions_lines(),
        "",
        *_availability_lines(),
    ]
    target = resume_dir(base) / f"resume-plan-{_slug(session_id or 'unknown')}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def _is_under_tmp(repo: Path) -> bool:
    tmp_roots = {Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve()}  # noqa: S108 — /tmp root compare
    resolved = repo.resolve()
    return any(resolved == root or root in resolved.parents for root in tmp_roots)


def _worktree_at_risk(repo: Path, *, porcelain: str) -> bool:
    """At-risk iff it has uncommitted changes AND is on volatile/unpushed ground.

    Volatile = under a ``/tmp`` root; unpushed = no upstream, or the branch is
    ahead of its upstream. A clean tree has nothing to lose (handled by the
    caller as a no-op).
    """
    if not porcelain.strip():
        return False
    has_upstream = bool(_git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"))
    ahead = bool(_git(repo, "log", "@{u}..HEAD", "--oneline").strip()) if has_upstream else False
    return _is_under_tmp(repo) or not has_upstream or ahead


def handle_at_risk_worktree(cwd: str | Path) -> AtRiskWorktree | None:
    """Capture *cwd*'s working state for recovery when it is at-risk.

    Returns ``None`` for a non-git dir, a clean tree, or a not-at-risk
    worktree (all no-ops). Otherwise stages the full working tree (incl.
    untracked) into a throwaway index and records it as a commit under
    ``refs/t3-resume/<slug>`` in the shared object store. The real index and
    branch history are never touched, and the fixed ref name makes re-runs
    idempotent (a single ref, overwritten; no ``chore:`` commit ever lands on
    the branch). Recovery is git-object-native — never a serialized file.
    """
    repo = Path(cwd)
    if not (repo / ".git").exists():
        return None
    porcelain = _git(repo, "status", "--porcelain")
    head = _git(repo, "rev-parse", "HEAD")
    if not head or not _worktree_at_risk(repo, porcelain=porcelain):
        return None

    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD") or "(detached)"
    slug = _slug(str(repo.resolve()))
    ref = f"{_RESUME_REF_PREFIX}{slug}"

    tmp_index = Path(tempfile.gettempdir()) / f"t3-resume-index-{os.getpid()}-{slug}"
    env = {**os.environ, "GIT_INDEX_FILE": str(tmp_index)}
    try:
        _git(repo, "read-tree", "HEAD", env=env)
        _git(repo, "add", "-A", env=env)
        tree = _git(repo, "write-tree", env=env)
        if not tree:
            return None
        commit = _git(repo, "commit-tree", tree, "-p", head, "-m", "chore: t3 resume snapshot", env=env)
        if not commit:
            return None
        _git(repo, "update-ref", ref, commit)
        # Verify-by-re-read: only claim recovery once the ref resolves to the
        # commit we just wrote (#1192 resilience invariant).
        if _git(repo, "rev-parse", ref) != commit:
            return None
    finally:
        tmp_index.unlink(missing_ok=True)

    return AtRiskWorktree(
        path=repo,
        branch=branch,
        recovery_ref=ref,
        recovery_commit=commit,
        uncommitted=len([line for line in porcelain.splitlines() if line.strip()]),
    )


def prepare_stop(session_id: str, cwd: str, *, base: Path | None = None) -> StopSnapshotResult:
    """Refresh every recovery artifact for *session_id* / *cwd*, idempotently.

    Each phase is independent and best-effort: a failure in one (e.g. a git
    hiccup, an unreadable TODO store) is logged and never aborts the others, so
    a single flaky phase can't cost the whole snapshot. Safe to re-run — files
    and the resume ref are overwritten in place.
    """
    resolved_session = session_id or current_session_id()

    todos_path: Path | None = _safe(lambda: write_todo_mirror(resolved_session, base=base), "todo-mirror")
    resume_plan_path: Path | None = _safe(lambda: write_resume_plan(resolved_session, cwd, base=base), "resume-plan")
    at_risk: list[AtRiskWorktree] = []
    handled = _safe(lambda: handle_at_risk_worktree(cwd), "at-risk-worktree") if cwd else None
    if handled is not None:
        at_risk.append(handled)

    return StopSnapshotResult(
        session_id=resolved_session,
        todos_path=todos_path,
        resume_plan_path=resume_plan_path,
        at_risk=at_risk,
    )


def _safe[T](fn: Callable[[], T], label: str) -> T | None:
    try:
        return fn()
    except Exception:
        logger.exception("stop_snapshot: %s phase failed — continuing", label)
        return None


__all__ = [
    "AtRiskWorktree",
    "StopSnapshotResult",
    "handle_at_risk_worktree",
    "prepare_stop",
    "resume_dir",
    "write_resume_plan",
    "write_todo_mirror",
]
