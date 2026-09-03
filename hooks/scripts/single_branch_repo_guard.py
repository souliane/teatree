"""PreToolUse: refuse a second branch or worktree in a repo declared single-branch.

Some repos are ONE branch wide for a stretch of their life — a fork bootstrap
where everything lands on one long-lived integration branch behind one open PR,
because there is no reviewed history yet to base a second branch on. Stating that
in prose did not hold it: the two repos carrying the rule accumulated 31
worktrees over 37 local branches, two of which re-implemented a fix already in
flight on the integration branch. Same work, twice, while the rule sat written
down three times over.

This gate closes the RAW path — ``git worktree add``, ``git checkout -b``,
``git switch -c``, ``git branch <name>``, and a ``git push`` whose destination is
a branch other than the pinned one. The ``t3 <overlay> worktree provision`` path
is closed separately inside
:meth:`~teatree.core.runners.provision.WorktreeProvisioner._provision_repo`, which
has the repo and branch as data and does not need to parse a command string.

Which repos, and which branch each is pinned to, is config — the
``single_branch_repos`` setting, ``<repo-slug>=<branch>`` entries. Removing an
entry is how the rule ends when the integration PR merges.

The classification is the pure core
:mod:`teatree.core.gates.single_branch_repo_guard`; this module supplies only the
environmental facts (which repo the command targets) and the deny emission. The
router helpers are imported lazily because ``hook_router`` imports this module at
top level, so a top-level back-import would cycle.

NEVER-LOCKOUT: a per-call ``[single-branch-ok: <reason>]`` token, the
``[teatree] single_branch_repo_gate_enabled = false`` kill-switch, and the shared
``_fail_open_or_deny`` chain all keep this gate from wedging a session. Every
resolution failure fails OPEN — a gate that blocks what it cannot read teaches its
operator to disable it.
"""

import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from hooks.scripts.managed_repo import teatree_src_on_path

if TYPE_CHECKING:
    from teatree.core.gates.single_branch_repo_guard import SingleBranchFinding

# Alias the bare and ``hooks.scripts.`` identities to ONE module object so the
# handler the router registers and a test patching a helper here operate on the
# same module — the pattern every sibling gate uses.
sys.modules.setdefault("single_branch_repo_guard", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.single_branch_repo_guard", sys.modules[__name__])

_SINGLE_BRANCH_OK_RE = re.compile(r"\[single-branch-ok:\s*(\S[^\]]*?)\s*\]")


def _gate_enabled() -> bool:
    """Whether the single-branch gate is enabled (default True); ``false`` is the kill-switch."""
    from hooks.scripts.hook_router import _teatree_bool_setting  # noqa: PLC0415 deferred back-import

    return _teatree_bool_setting("single_branch_repo_gate_enabled", default=True)


def _ok_token(command: str) -> str | None:
    """The reason from a ``[single-branch-ok: <reason>]`` token in *command*, else None."""
    match = _SINGLE_BRANCH_OK_RE.search(command[:512])
    if not match:
        return None
    return match.group(1).strip() or None


def _declared_entries() -> list[str]:
    """The ``single_branch_repos`` entries, or ``[]`` when unreadable (gate inert).

    Read through the Django-free cold reader, as every other cold hook does. This runs as a
    PreToolUse hook, where Django is not configured — resolving the declaration through the
    ORM chokepoint raises there, and the fail-open below would then silently leave the gate
    inert in precisely the environment it exists to guard.
    """
    try:
        with teatree_src_on_path():
            from teatree.config.cold_reader import read_setting  # noqa: PLC0415 — deferred: cold-hook import

            declared = read_setting("single_branch_repos")
    except Exception:  # noqa: BLE001 — a cold env without teatree fails OPEN (no entries → inert).
        return []
    return [str(entry) for entry in declared] if isinstance(declared, list) else []


def _load_core():  # noqa: ANN202 — returns a lazily-imported handle; annotating would pull the type to module scope
    """The decision core, or ``None`` on any import failure (caller then allows)."""
    try:
        with teatree_src_on_path():
            from teatree.core.gates import single_branch_repo_guard as core  # noqa: PLC0415 — cold-hook import
    except Exception:  # noqa: BLE001 — a cold env without teatree fails OPEN, never tracebacks.
        return None
    return core


def _target_repo_dir(command: str, cwd: "Path | None") -> "Path | None":
    """The dir whose repo *command* targets, or ``None`` when unresolvable.

    Keys off the command's EFFECTIVE dir (honouring a leading ``cd`` and git's
    ``-C``/``--git-dir`` redirection) rather than the ambient cwd, for the same
    reason the main-clone gate does: ``git -C <pinned-repo> checkout -b x`` run
    from elsewhere still creates a second branch in the pinned repo, and a
    redirection AWAY from it must not be falsely denied.
    """
    try:
        with teatree_src_on_path():
            from teatree.hooks._commit_repo_dir import resolve_commit_dir  # noqa: PLC0415, PLC2701 — cold-hook import

            resolved = resolve_commit_dir(command, cwd)
            if not isinstance(resolved, Path):
                return None
            return resolved.parent if resolved.name == ".git" else resolved
    except Exception:  # noqa: BLE001 — cannot pin the repo → fail OPEN.
        return None


def _target_repo_slug(command: str, cwd: "Path | None") -> str:
    """The remote slug of the repo *command* targets, or ``""`` when unresolvable."""
    repo_dir = _target_repo_dir(command, cwd)
    if repo_dir is None:
        return ""
    try:
        with teatree_src_on_path():
            from teatree.utils.git import remote_url  # noqa: PLC0415 — deferred: cold-hook import

            return remote_url(str(repo_dir)) or ""
    except Exception:  # noqa: BLE001 — cannot pin the repo → fail OPEN.
        return ""


def _current_branch(repo_dir: "Path | None") -> str:
    """The branch *repo_dir* has checked out, or ``""`` when unresolvable/detached.

    The one fact a refspec-less ``git push`` needs to be decidable; ``""`` keeps
    the core's allow, so an unreadable repo never turns into a false refusal.
    """
    if repo_dir is None:
        return ""
    try:
        with teatree_src_on_path():
            from teatree.utils.run import run_checked  # noqa: PLC0415 — deferred: cold-hook import

            result = run_checked(["git", "-C", str(repo_dir), "symbolic-ref", "--quiet", "--short", "HEAD"])
    except Exception:  # noqa: BLE001 — detached HEAD, not a repo, or git unavailable → unresolved.
        return ""
    return result.stdout.strip()


def _push_branch_is_local_to(repo_dir: "Path | None", branch: str) -> bool:
    """Whether *branch* is a real local ref in *repo_dir* — the push gate's premise check.

    A push is the one surface whose premise can be CHECKED rather than inferred.
    The cwd a hook is handed is the SESSION dir, not the command's, so a push of
    another repo's branch would otherwise be refused as a second branch here.
    An unproven premise fails OPEN: a push of a nonexistent branch fails anyway.
    """
    if repo_dir is None or not branch:
        return False
    try:
        with teatree_src_on_path():
            from teatree.utils.run import run_checked  # noqa: PLC0415 — deferred: cold-hook import

            run_checked(["git", "-C", str(repo_dir), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"])
    except Exception:  # noqa: BLE001 — no such ref, not a repo, or git unavailable → premise unproven.
        return False
    return True


def _finding(core, data: dict) -> "tuple[SingleBranchFinding, str, str] | None":  # noqa: ANN001 — duck-typed handle passed positionally
    """``(finding, pinned_branch, repo)`` for a refused creation, else None."""
    from hooks.scripts.hook_router import _resolve_cwd_repo  # noqa: PLC0415 deferred back-import

    command = data.get("tool_input", {}).get("command", "")
    if not isinstance(command, str) or not command:
        return None
    entries = _declared_entries()
    if not entries:
        return None
    cwd = _resolve_cwd_repo(data)
    repo_dir = _target_repo_dir(command, cwd)
    repo = _target_repo_slug(command, cwd)
    if not repo:
        return None
    pinned = core.resolve_pinned_branch(repo, entries)
    if not pinned:
        return None
    found = core.find_second_branch_creation(command, pinned_branch=pinned, current_branch=_current_branch(repo_dir))
    # A push is the one surface whose premise can be CHECKED rather than inferred.
    if found is None or (found.surface == "push" and not _push_branch_is_local_to(repo_dir, found.target)):
        return None
    return found, pinned, repo


def _gate_should_skip(data: dict) -> bool:
    """True iff a pre-check says this call is out of scope or explicitly escaped.

    Mirrors :func:`hooks.scripts.main_clone_guard._gate_should_skip` — the
    pre-checks live here rather than inline so the handler stays a short,
    readable chain of the decisions that actually matter.
    """
    if data.get("tool_name", "") != "Bash" or not _gate_enabled():
        return True
    tool_input = data.get("tool_input", {})
    if not isinstance(tool_input, dict):
        return True
    command = tool_input.get("command", "")
    if not isinstance(command, str) or not command:
        return True
    if reason := _ok_token(command):
        sys.stderr.write(f"NOTE: single-branch gate skipped via [single-branch-ok: {reason}].\n")
        return True
    return False


def handle_block_second_branch(data: dict) -> bool:
    """Deny a second-branch/worktree creation in a repo declared single-branch.

    Returns ``True`` (deny emitted) for ``git worktree add`` / ``checkout -b`` /
    ``switch -c`` / ``branch <name>`` / a ``push`` to a non-pinned branch in such a
    repo; ``False`` (allow) for everything else — commits on the pinned branch,
    ``worktree list``/``remove``/``prune``, branch DELETION (that is how the repo
    is brought back into compliance), fetch, pull, and all read-only git.
    """
    from hooks.scripts.hook_router import _fail_open_or_deny  # noqa: PLC0415 deferred back-import

    if _gate_should_skip(data):
        return False
    core = _load_core()
    if core is None:
        return False
    resolved = _finding(core, data)
    if resolved is None:
        return False
    finding, pinned, repo = resolved
    return _fail_open_or_deny(data, core.deny_reason(finding, pinned_branch=pinned, repo=repo))
