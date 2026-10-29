"""Refuse a raw interactive push into a bot namespace before it reaches git."""

from typing import Any

from hooks.scripts.foreign_branch_push_argv import PushSpec
from hooks.scripts.foreign_branch_push_git import git_probe
from hooks.scripts.foreign_branch_push_read import ParsedPushes, push_specs
from hooks.scripts.managed_repo import teatree_src_on_path


def handle_block_bot_push(data: dict[str, Any], *, parsed: ParsedPushes | None = None) -> bool:
    if data.get("tool_name") != "Bash":
        return False
    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return False
    if parsed is None:
        parsed = push_specs(command, str(data.get("cwd") or ""))
    if not parsed.specs:
        return False

    from hooks.scripts.hook_router import emit_pretooluse_deny  # noqa: PLC0415 — router imports this hook

    with teatree_src_on_path():
        from teatree.hooks.bot_push_gate import (  # noqa: PLC0415 — cold src path
            BotIdentityUnreadableError,
            bot_repo_for,
        )

    for spec in parsed.specs:
        if spec.unpinnable:
            continue
        remote_url = _push_url(spec)
        if not remote_url:
            continue
        try:
            target = bot_repo_for(remote_url)
        except BotIdentityUnreadableError:
            return emit_pretooluse_deny("REFUSED: bot identity configuration could not be read", gate_id="bot_push")
        if target is not None:
            return emit_pretooluse_deny(
                f"REFUSED: raw git push to bot-owned {target.slug}; "
                "use `t3 push` to verify the bot credential and commits",
                gate_id="bot_push",
            )
    return False


def _push_url(spec: PushSpec) -> str:
    overrides = [item for value in spec.config_overrides for item in ("-c", value)]
    remote = git_probe(spec.work_dir, *overrides, "remote", "get-url", "--push", spec.remote)
    if not remote.ok:
        remote = git_probe(spec.work_dir, *overrides, "ls-remote", "--get-url", "--", spec.remote)
    return remote.out if remote.ok else ""
