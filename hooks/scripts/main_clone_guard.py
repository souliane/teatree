"""PreToolUse: block-main-clone-mutation gate (#2836).

A teatree main clone (a primary ``.git``-*dir* checkout) is for branching
worktrees from — never for editing. The recorded incident: a sub-agent ran
``git checkout <feature-branch>`` inside the main clone, leaving it detached,
eight commits behind ``origin/main``, and dirty. ``t3`` runs the editable
install FROM that clone, so it then executed stale code, and the housekeeping
self-update could not fast-forward the dirty/detached tree — teatree went
silently stale.

This gate denies a working-tree mutation of any REGISTERED (teatree-managed)
main clone — an ``Edit``/``Write`` to a path under it, or a ``git
checkout``/``switch`` to a non-default branch / ``reset --hard`` / ``restore``
/ ``stash pop`` in a main-clone cwd — while ALLOWING ``git fetch``, ``git pull
--ff-only``, ``git checkout <default>``, ``git worktree add/remove/prune/list``,
and all read-only git, so ``t3 update`` and worktree creation keep working. The
verdict is UNIVERSAL — even the repo owner must branch a worktree.

The managed-repo / worktree-vs-clone resolution reuses the shared
``managed_repo`` toolkit (``running_from_worktree`` distinguishes a linked
worktree's ``.git`` *file* from a primary clone's ``.git`` *dir*); paths are
canonicalised before comparing. The git-command classification is the pure core
:mod:`teatree.core.gates.main_clone_guard`. The router helpers
(``_fail_open_or_deny``, ``_teatree_bool_setting``, ``_resolve_cwd_repo``) are
imported lazily — ``hook_router`` imports this module at top level, so a
top-level back-import would cycle.

NEVER-LOCKOUT: a per-call ``[main-clone-ok: <reason>]`` token, the
``[teatree] main_clone_guard_gate_enabled = false`` kill-switch, and the shared
``_fail_open_or_deny`` chain (self-rescue allowlist + master fail-open +
circuit breaker) all keep this gate from wedging a session.
"""

import contextlib
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from hooks.scripts.managed_repo import (
    default_branch,
    file_is_inside_worktree,
    is_agent_state_path,
    load_protected_branches,
    repo_root_is_teatree_managed,
    resolve_branch_and_root,
    teatree_src_on_path,
)

if TYPE_CHECKING:
    from teatree.core.gates.main_clone_guard import MainCloneFinding

# Alias the bare and ``hooks.scripts.`` identities to ONE module object so the
# handler the router registers and a test patching a helper here operate on the
# same module — the pattern every sibling uses.
sys.modules.setdefault("main_clone_guard", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.main_clone_guard", sys.modules[__name__])

_MAIN_CLONE_OK_RE = re.compile(r"\[main-clone-ok:\s*(\S[^\]]*?)\s*\]")
_FILE_TOOLS = {"Write", "Edit"}


def _gate_enabled() -> bool:
    """Whether the main-clone mutation gate is enabled (default True).

    Fails OPEN to enabled on a missing/broken config; an explicit ``false``
    (``[teatree] main_clone_guard_gate_enabled = false``) is the kill-switch.
    """
    from hooks.scripts.hook_router import _teatree_bool_setting  # noqa: PLC0415 deferred back-import

    return _teatree_bool_setting("main_clone_guard_gate_enabled", default=True)


def _ok_token(data: dict) -> str | None:
    """Return the reason from a ``[main-clone-ok: <reason>]`` token, else None."""
    tool_input = data.get("tool_input", {})
    if not isinstance(tool_input, dict):
        return None
    for field in ("content", "new_string", "file_path", "command"):
        value = tool_input.get(field, "")
        if not isinstance(value, str) or not value:
            continue
        match = _MAIN_CLONE_OK_RE.search(value[:512])
        if not match:
            continue
        reason = match.group(1).strip()
        if reason:
            return reason
    return None


def _load_core():  # noqa: ANN202 — returns a lazily-imported handle; annotating would pull the type to module scope
    """Import the decision core, bootstrapping the sibling ``src/`` onto the path.

    Returns the core module, or ``None`` on any import failure — the caller then
    fails OPEN (allow), so a cold hook env without ``teatree`` never tracebacks.
    """
    src_dir = Path(__file__).resolve().parents[2] / "src"
    added = False
    try:
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
            added = True
        from teatree.core.gates import main_clone_guard as core  # noqa: PLC0415 — deferred: cold-hook import
    except Exception:  # noqa: BLE001 — a cold env without teatree fails OPEN, never tracebacks.
        return None
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(str(src_dir))
    return core


def _is_managed_main_clone(repo_root: str) -> bool:
    """True iff *repo_root* is a REGISTERED (teatree-managed) primary clone.

    Resolves symlinks first, then reuses the shared worktree helper: a linked
    worktree (``.git`` *file*) is NOT a main clone (work belongs there — allow),
    and only a ``.git``-*dir* primary clone that is teatree-managed qualifies.
    Any resolution error fails OPEN (return ``False``).
    """
    try:
        root = Path(repo_root).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    try:
        with teatree_src_on_path():
            from teatree.paths import running_from_worktree  # noqa: PLC0415 — deferred: cold-hook import

            if running_from_worktree(root):
                return False
    except Exception:  # noqa: BLE001 — cannot confirm worktree-vs-clone → fail OPEN.
        return False
    if not (root / ".git").is_dir():
        return False
    return repo_root_is_teatree_managed(str(root))


def _edit_finding(core, data: dict) -> "MainCloneFinding | None":  # noqa: ANN001 — untyped by design: a duck-typed handle passed positionally
    """Resolve an Edit/Write landing on a path under a managed main clone, else None."""
    file_path = data.get("tool_input", {}).get("file_path", "")
    if not isinstance(file_path, str) or not file_path or is_agent_state_path(file_path):
        return None
    resolved = resolve_branch_and_root(str(Path(file_path).expanduser().parent))
    if resolved is None:
        return None
    _branch, repo_root = resolved
    if not _is_managed_main_clone(repo_root) or not file_is_inside_worktree(repo_root, file_path):
        return None
    return core.edit_finding(file_path)


def _effective_command_dir(command: str, cwd: "Path | None") -> "Path | None":
    """Resolve the dir whose repo a git command actually targets, else the cwd.

    Honours a leading ``cd``/``pushd`` and git's ``-C``/``--git-dir``
    redirection. The gate must key off the repo the command MUTATES, not the ambient cwd:
    ``git -C <main-clone> checkout feature`` run from a worktree cwd mutates the
    MAIN CLONE (it must block), and ``git -C <worktree> checkout feature`` run
    from a main-clone cwd mutates the WORKTREE (it must allow). Reuses the
    canonical static resolver :func:`teatree.hooks._commit_repo_dir.resolve_commit_dir`
    (cumulative ``-C``, last-wins ``--git-dir``, leading ``cd``; ``--work-tree``
    correctly never selects the repo), so this gate and the publish gate agree
    on git's directory semantics.

    A ``--git-dir <X>/.git`` value resolves to the metadata dir; normalise it to
    its enclosing repo root (``X``) so :func:`resolve_branch_and_root` can run
    ``git`` from a working tree. Returns the unchanged *cwd* for a plain command
    with no redirection, or ``None`` when the target cannot be pinned statically
    (a substitution marker) — failing OPEN rather than guessing a repo.

    LIMITATION (pinned by test): the bare ``GIT_DIR=<X>/.git git …`` /
    ``GIT_WORK_TREE=`` ENVIRONMENT-variable redirection forms are not parsed
    (only the ``-C``/``--git-dir`` ARG forms are), so they fall back to
    cwd-keying — the common ``-C`` form, the recorded incident shape, is fully
    handled.
    """
    try:
        with teatree_src_on_path():
            from teatree.hooks._commit_repo_dir import resolve_commit_dir  # noqa: PLC0415, PLC2701 — cold-hook import

            resolved = resolve_commit_dir(command, cwd)
    except Exception:  # noqa: BLE001 — a cold env without teatree fails OPEN to cwd-keying.
        return cwd
    if not isinstance(resolved, Path):
        # The ``UNRESOLVABLE_REPO_DIR`` str sentinel (a ``-C`` value we cannot
        # pin statically) or ``None`` (no redirect and no cwd) — either way
        # there is no repo to key off, so don't block.
        return None
    return resolved.parent if resolved.name == ".git" else resolved


def _git_finding(core, data: dict) -> "MainCloneFinding | None":  # noqa: ANN001 — untyped by design: a duck-typed handle passed positionally
    """Resolve a forbidden git command targeting a managed main clone, else None.

    The targeted repo is the command's EFFECTIVE dir (honouring ``cd`` / ``-C``
    / ``--git-dir`` redirection, :func:`_effective_command_dir`), not the
    ambient cwd, so a ``-C <main-clone>`` redirection cannot bypass the gate and
    a ``-C <worktree>`` redirection from a clone cwd is not falsely denied.
    """
    from hooks.scripts.hook_router import _resolve_cwd_repo  # noqa: PLC0415 deferred back-import

    command = data.get("tool_input", {}).get("command", "")
    if not isinstance(command, str) or not command:
        return None
    effective = _effective_command_dir(command, _resolve_cwd_repo(data))
    if effective is None:
        return None
    resolved = resolve_branch_and_root(str(effective))
    if resolved is None:
        return None
    _branch, repo_root = resolved
    if not _is_managed_main_clone(repo_root):
        return None
    return core.find_main_clone_git_mutation(
        command,
        default_branch=default_branch(Path(repo_root)),
        protected_branches=frozenset(load_protected_branches()),
    )


def _gate_should_skip(data: dict) -> bool:
    """True iff a pre-check says this call is out of scope or escaped."""
    if data.get("tool_name", "") not in {"Write", "Edit", "Bash"}:
        return True
    if not _gate_enabled():
        return True
    if reason := _ok_token(data):
        sys.stderr.write(f"NOTE: main-clone gate skipped via [main-clone-ok: {reason}].\n")
        return True
    return not isinstance(data.get("tool_input", {}), dict)


def handle_block_main_clone_mutation(data: dict) -> bool:
    """Deny a working-tree mutation of a registered (teatree-managed) main clone.

    Returns ``True`` (deny emitted) for an Edit/Write under a managed main clone
    or a forbidden git command (checkout/switch off-default, reset --hard,
    restore, stash pop) in a main-clone cwd; ``False`` (allow) for everything
    else — worktree edits, read-only git, fetch, pull --ff-only, checkout
    <default>, worktree add/remove/prune/list. Fail-open on every resolution
    failure; the deny routes through ``_fail_open_or_deny`` (never-lockout).
    """
    from hooks.scripts.hook_router import _fail_open_or_deny  # noqa: PLC0415 deferred back-import

    if _gate_should_skip(data):
        return False
    core = _load_core()
    if core is None:
        return False
    tool_name = data.get("tool_name", "")
    finding = _edit_finding(core, data) if tool_name in _FILE_TOOLS else _git_finding(core, data)
    if finding is None:
        return False
    return _fail_open_or_deny(data, core.deny_reason(finding))
