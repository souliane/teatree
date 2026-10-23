"""PreToolUse refusal for linked checkouts outside teatree's allowed roots."""

import re
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING

from hooks.scripts.managed_repo import repo_root_is_teatree_managed, teatree_src_on_path
from hooks.scripts.teatree_settings import teatree_bool_setting

if TYPE_CHECKING:
    from teatree.config.checkout_roots import CheckoutRoots

# A superset of what the scanner reports, so a miss can skip the scan without hiding a checkout.
_MAY_CHECKOUT = re.compile(r"worktree|(?<![\w.-])(?:sh|bash|zsh|eval)(?![\w-])")


def _gate_enabled() -> bool:
    return teatree_bool_setting("worktree_containment_gate_enabled")


def _may_checkout(command: str) -> bool:
    return _MAY_CHECKOUT.search(re.sub(r"[\"'\\]", "", command)) is not None or "$'" in command


def _deny_reason(raw_target: str, target: Path | None, policy: "CheckoutRoots") -> str:
    if target is None:
        names = ", ".join(str(root) for root in (*policy.durable, *policy.disposable))
        location = f"unresolved target {raw_target!r}; allowed roots: {names}"
    else:
        location = policy.refusal(target)
    return (
        f"BLOCKED: git worktree target {location} "
        "Use `t3 <overlay> workspace ticket <issue>` or choose an allowed temp root for disposable work. "
        "One-call waiver: "
        "`[gate-escape:worktree_containment: <reason>]` in the Bash command."
    )


def handle_block_worktree_checkout(data: dict) -> bool:
    if data.get("tool_name") != "Bash":
        return False
    command = data.get("tool_input", {}).get("command", "")
    if not isinstance(command, str) or not _may_checkout(command) or not _gate_enabled():
        return False
    raw_cwd = data.get("cwd")
    cwd = Path(raw_cwd) if isinstance(raw_cwd, str) and raw_cwd else None
    with teatree_src_on_path():
        core = import_module("teatree.core.gates.worktree_containment")
        repo_dir = import_module("teatree.hooks._commit_repo_dir")
        policy = None
        for call in core.scan_worktree_checkouts(command, cwd):
            if call.source is None or not call.source.is_dir():
                continue
            clone = repo_dir.git_root_for_dir(call.source)
            if clone is None or not repo_root_is_teatree_managed(str(clone)):
                continue
            if policy is None:
                policy = import_module("teatree.config.checkout_roots").checkout_roots()
            if call.target is not None and policy.allows(call.target):
                continue
            from hooks.scripts.hook_router import _fail_open_or_deny  # noqa: PLC0415 — deferred router back-import

            return _fail_open_or_deny(
                data, _deny_reason(call.raw_target, call.target, policy), gate_id="worktree_containment"
            )
    return False


__all__ = ["handle_block_worktree_checkout"]
