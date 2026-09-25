"""The agent-spawn headroom gauge for ``t3 doctor check`` (#4301).

``execve`` refuses argv+envp past ``ARG_MAX`` with no partial degradation — the agent
either spawns or the phase is dead — and nothing warned as the box approached it. This
reports how much of that budget a dispatch already spends, so the cliff reads as a trend.

It measures a FLOOR: the ambient environment every child inherits plus the argv of a
baseline ``ClaudeAgentOptions``. A real dispatch adds per-phase flags on top, and since
#4301 the system prompt is no longer among them (it travels as a file), so the floor is
dominated by the environment — which is exactly the term a per-argument check cannot see.
"""

import os

import typer

#: Advisory band. A spawn floor spending most of the budget leaves a real dispatch's
#: per-phase flags nowhere to go, and the failure when it lands is total.
_WARN_PERCENT = 70.0


def _check_agent_spawn_headroom() -> bool:
    """WARN as the spawn payload approaches ``ARG_MAX``, FAIL once the floor exceeds it.

    A floor already over the limit means EVERY dispatch on this box is dead before it
    starts, which is a broken product rather than an advisory — so that band hard-FAILs.
    Crash-proof: any probe error degrades to a silent pass, so the gauge never aborts a
    doctor run.
    """
    try:
        # Deferred to the check body, not the module. `t3 doctor` is one command, but
        # this module is reached from `teatree.cli.doctor`'s package import, so a
        # module-scope `claude_agent_sdk` here is paid by EVERY `t3` invocation —
        # MEASURED as the second of the two paths that pulled the SDK (and `mcp`
        # behind it) into CLI startup. `claude_cli_spawn` imports the SDK itself, so
        # both names have to move together or the chain survives.
        from claude_agent_sdk import ClaudeAgentOptions  # noqa: PLC0415 — deferred: keeps the SDK off CLI startup

        from teatree.agents.claude_cli_spawn import (  # noqa: PLC0415 — deferred: imports the SDK itself
            preflight_payload,
        )
        from teatree.agents.compaction_guard import with_compaction_off  # noqa: PLC0415 — deferred: imports the SDK

        payload = preflight_payload(with_compaction_off(ClaudeAgentOptions()), dict(os.environ))
    except Exception:  # noqa: BLE001 — a diagnostic must never abort the doctor run
        return True
    if payload.total_bytes > payload.total_limit_bytes:
        typer.echo(
            f"FAIL  agent spawn payload exceeds this host's limit — every dispatch dies at "
            f"execve with E2BIG before doing any work. {payload.gauge()}. Shrink the child "
            "environment, or raise the stack rlimit (ARG_MAX is the stack limit / 4)."
        )
        return False
    if payload.used_percent >= _WARN_PERCENT:
        typer.echo(
            f"WARN  agent spawn payload is close to this host's limit; past it a dispatch cannot "
            f"start at all. {payload.gauge()}"
        )
    return True
