"""PreToolUse: protect-default-branch.

Extracted whole out of ``hook_router`` (shrink-only over its module-health cap) to
offset the ``raw_ticket_ignore_loop_gate`` registration added in the same change —
see ``docs/module-health.md``'s extract-first rule. Behaviour is unchanged; only
the home moved.

Cold-import safe: the live PreToolUse hook is a bare ``python3`` subprocess with
no guarantee ``teatree`` is importable, so the module top imports only stdlib and
the already-extracted ``managed_repo`` sibling — never Django / ``teatree.core``.
The deny routes through the router's shared ``_fail_open_or_deny`` chokepoint
(back-imported lazily; the writer + circuit breaker stay in the router).
"""

import sys
from pathlib import Path

from hooks.scripts.managed_repo import file_is_inside_worktree as _file_is_inside_worktree
from hooks.scripts.managed_repo import is_agent_state_path as _is_agent_state_path
from hooks.scripts.managed_repo import load_protected_branches as _load_protected_branches
from hooks.scripts.managed_repo import repo_root_is_teatree_managed as _repo_root_is_teatree_managed
from hooks.scripts.managed_repo import resolve_branch_and_root as _resolve_branch_and_root

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# re-exports and a test patching a helper here operate on ONE module object.
sys.modules.setdefault("protect_default_branch_guard", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.protect_default_branch_guard", sys.modules[__name__])

_FILE_PATH_TOOLS = {"Edit", "Write"}


def handle_protect_default_branch(data: dict) -> bool:
    """Block Edit/Write on a source file in a teatree-MANAGED protected-branch repo.

    Scoped to the TARGET FILE's own repo, never to the cwd's branch and
    never to "any git repo" (#126). The block fires only when ALL hold:

    1. the tool is ``Edit``/``Write`` with a ``file_path``;
    2. the path is NOT agent-harness state (memory / todos / per-project
        state) — those are git-tracked scratch state, never protected
        source, so they are exempt even on ``main``;
    3. the file's enclosing git repo is on a protected branch;
    4. the file genuinely lives inside that repo's working tree;
    5. that repo is teatree-MANAGED (core + the active overlay's
        registered repos) — an unmanaged repo on ``main`` (a dotfiles
        clone, an unrelated project) is NOT this gate's concern.

    Any condition unmet → allow (fail open). A git error, an
    unresolvable repo, or an unclassifiable slug all allow — the
    gate-over-deny class this change closes means uncertainty errs toward
    letting the write through, not blocking it.
    """
    from hooks.scripts.hook_router import _fail_open_or_deny  # noqa: PLC0415 deferred back-import

    tool_name = data.get("tool_name", "")
    file_path = data.get("tool_input", {}).get("file_path", "")
    # Agent-harness state is never repo source — allow it even on `main`.
    if tool_name not in _FILE_PATH_TOOLS or not file_path or _is_agent_state_path(file_path):
        return False

    resolved = _resolve_branch_and_root(str(Path(file_path).parent))
    if resolved is None:
        return False
    branch, repo_root = resolved

    if (
        branch not in _load_protected_branches()
        or not _file_is_inside_worktree(repo_root, file_path)
        or not _repo_root_is_teatree_managed(repo_root)
    ):
        return False

    return _fail_open_or_deny(
        data,
        f"BLOCKED: file is on protected branch '{branch}' in a teatree-managed repo. "
        "Create a worktree first with `t3 teatree workspace ticket`.",
    )
