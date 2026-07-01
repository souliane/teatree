"""Register one native Claude ``/loop`` per ENABLED DB ``Loop`` row (#2650).

Bare sibling of ``hook_router`` (hooks/CLAUDE.md: NEW hook logic lives in a
sibling module, never in the shrink-only-capped router). The owner session's
``UserPromptSubmit`` handler delegates here to emit ONE ``register_cron``
directive per ENABLED ``Loop`` row — so the live set of native Claude ``/loop``s
MIRRORS the set of enabled rows (per-loop, not per-group), replacing the single
fat-tick cron the owner used to register.

The directive source of truth is the seam ``teatree.loops.claude_specs`` (the
SAME mapping the ``/t3:loops`` enable/disable skill reads via ``t3 loop
claude-spec``), so the hook and the skill can never disagree on a loop's
``slot_id`` / ``cron`` / ``prompt``.

Crash-proof / fail-open / silent: any failure to bootstrap Django or query the
seam yields ZERO specs, so the handler stays silent — never an exception into the
30s ``UserPromptSubmit`` hook. Zero enabled loops likewise emits nothing (there is
nothing to register).
"""

import json
import re
import sys
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from teatree.loops.claude_specs import ClaudeLoopSpec

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# imports and a test patching a helper here operate on ONE module object.
sys.modules.setdefault("loop_registrations", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.loop_registrations", sys.modules[__name__])


class _Writable(Protocol):
    def write(self, text: str, /) -> object: ...


# The per-loop run command + its full bare-prompt shape, kept in sync with the
# seam ``teatree.loops.claude_specs.loop_run_prompt`` (a parity test pins the two
# together so they cannot drift). Used to RECOGNISE a fired per-loop tick prompt
# from the hot ``UserPromptSubmit`` path WITHOUT importing teatree (no Django).
_RUN_CMD_RE = re.compile(r"t3 loops tick --loop (?P<name>[^\s`]+)")
_BARE_PROMPT_RE = re.compile(r"^Run `t3 loops tick --loop \S+` in Bash, then briefly report the tick summary\.$")


def _enabled_loop_specs() -> "list[ClaudeLoopSpec]":
    """The enabled-loop specs from the seam; fail-open to ``[]`` on ANY error."""
    try:
        from django_bootstrap import bootstrap_teatree_django  # noqa: PLC0415

        if not bootstrap_teatree_django():
            return []
        from teatree.loops.claude_specs import enabled_loop_specs  # noqa: PLC0415

        return list(enabled_loop_specs())
    except Exception:  # noqa: BLE001 — fast hook must never raise; silent fail-open.
        return []


def loop_registration_directives() -> list[dict]:
    """One ``register_cron`` payload per enabled loop — the exact ``CronCreate`` args."""
    return [
        {"slot_id": spec.slot_id, "cron": spec.cron, "prompt": spec.prompt, "recurring": True}
        for spec in _enabled_loop_specs()
    ]


def emit_loop_registrations(stream: _Writable) -> bool:
    """Emit the per-enabled-loop register directives + a prose fallback.

    Returns whether anything was emitted. Zero enabled loops (or a fail-open empty
    seam) emits NOTHING and returns ``False`` — there is nothing to register, so
    the owner session stays silent.
    """
    loops = loop_registration_directives()
    if not loops:
        return False
    # Structured directive for a harness that auto-registers from
    # ``hookSpecificOutput`` (best-effort, carries the full per-loop list); the
    # prose below is the reliable fallback the agent reads to CronCreate each.
    directive = {"hookSpecificOutput": {"action": "register_cron", "recurring": True, "loops": loops}}
    json.dump(directive, stream)
    stream.write("\n")
    _write_prose(loops, stream)
    return True


def _write_prose(loops: list[dict], stream: _Writable) -> None:
    stream.write(
        f"Session setup: the teatree background loops are not registered yet. Register one native "
        f"Claude `/loop` per enabled loop ({len(loops)} enabled) — call CronCreate for each:\n"
    )
    for entry in loops:
        stream.write(
            f'  - {entry["slot_id"]}: CronCreate(cron="{entry["cron"]}", prompt="{entry["prompt"]}", recurring=true)\n'
        )


def is_bare_loop_tick_prompt(stripped: str) -> bool:
    """True when *stripped* is a PURE per-loop tick prompt (#2650) — no residual user content."""
    return bool(_BARE_PROMPT_RE.match(stripped))


def loop_name_from_prompt(prompt: str) -> str | None:
    """The ``--loop <name>`` a per-loop tick prompt runs, or ``None`` when it is not one."""
    match = _RUN_CMD_RE.search(prompt)
    return match.group("name") if match else None
