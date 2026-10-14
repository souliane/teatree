"""PreToolUse refusal for linked checkouts outside teatree's allowed roots."""

import re
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING

from hooks.scripts.managed_repo import repo_root_is_teatree_managed, teatree_src_on_path
from hooks.scripts.teatree_settings import teatree_bool_setting

if TYPE_CHECKING:
    from teatree.config.checkout_roots import CheckoutRoots

_WAIVER = re.compile(r"\[worktree-containment-ok:\s*(\S[^\]]*?)\s*\]")


def _gate_enabled() -> bool:
    return teatree_bool_setting("worktree_containment_gate_enabled", default=True)


def _deny_reason(raw_target: str, target: Path | None, roots: "CheckoutRoots") -> str:
    raw_policy = replace(roots, harness=())
    if target is None:
        names = ", ".join(str(root) for root in (*roots.durable, *roots.disposable))
        location = f"unresolved target {raw_target!r}; allowed roots: {names}"
    else:
        location = raw_policy.refusal(target)
    return (
        f"BLOCKED: git worktree target {location} "
        "Use `t3 <overlay> workspace ticket <issue>` or choose an allowed temp root for disposable work. "
        "One-call waiver: "
        "`[worktree-containment-ok: <reason>]` in the Bash command."
    )


def handle_block_worktree_checkout(data: dict) -> bool:
    if data.get("tool_name") != "Bash":
        return False
    command = data.get("tool_input", {}).get("command", "")
    if not isinstance(command, str) or not command or _WAIVER.search(command) or not _gate_enabled():
        return False
    raw_cwd = data.get("cwd")
    cwd = Path(raw_cwd) if isinstance(raw_cwd, str) and raw_cwd else None
    with teatree_src_on_path():
        core = import_module("teatree.core.gates.worktree_containment")
        placement = import_module("teatree.config.checkout_roots")
        repo_dir = import_module("teatree.hooks._commit_repo_dir")
        for call in core.scan_worktree_checkouts(command, cwd):
            if call.source is None or not call.source.is_dir():
                continue
            clone = repo_dir.git_root_for_dir(call.source)
            if clone is None or not repo_root_is_teatree_managed(str(clone)):
                continue
            roots = placement.checkout_roots(source_clone=clone)
            if call.target is not None and any(
                call.target.is_relative_to(root) for root in (*roots.durable, *roots.disposable)
            ):
                continue
            from hooks.scripts.hook_router import _fail_open_or_deny  # noqa: PLC0415 — deferred router back-import

            return _fail_open_or_deny(
                data, _deny_reason(call.raw_target, call.target, roots), gate_id="worktree_containment"
            )
    return False


__all__ = ["handle_block_worktree_checkout"]
