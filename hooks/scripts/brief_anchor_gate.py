"""Lint a sub-agent brief for unanchored factual assertions at dispatch time (#4341).

An orchestrator cannot know in advance WHICH of its assertions is stale — that is
the whole failure mode. This gate does not need to know either: it only checks that
the brief either anchors its claims to a commit or tells the sub-agent it may
overrule them, and quotes the one-sentence remedy when it does neither.

WARN, not deny, by default. The fleet dispatches constantly and every brief passes
through here, so a false deny on an ordinary brief costs far more than a line the
reader skips; the enforcement value is the remedy text, not the block. The refuse
posture is opt-in behind ``brief_anchor_gate_refuse`` and routes through the shared
``_fail_open_or_deny`` chokepoint, so it inherits the self-rescue allowlist and the
master ``danger_gate_fail_open`` switch like every other over-deny gate.

NEVER-LOCKOUT: the default posture cannot deny at all; the
``brief_anchor_gate_enabled`` kill-switch (``t3 <overlay> gate brief-anchor
disable``) turns it off entirely; a per-call ``[brief-anchor-ok: <reason>]`` token
in the first 512 chars of the brief clears one dispatch (an empty reason does not);
and any internal error, an unimportable ``teatree`` and an unreadable settings store
all fail OPEN.

Cold-import safe: the module top imports stdlib only — ``teatree`` is reached
through a deferred import after the ``sys.path`` bootstrap.
"""

import contextlib
import re
import sys
from pathlib import Path

# Alias the bare and ``hooks.scripts.`` identities so a test patching a helper here
# and the live hook's own import operate on ONE module object.
sys.modules.setdefault("brief_anchor_gate", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.brief_anchor_gate", sys.modules[__name__])

#: The two harness tool names that spawn a sub-agent.
DISPATCH_TOOLS = frozenset({"Agent", "Task"})

# Per-call opt-out, mirroring ``[admission-ok: <reason>]``. An empty reason does not clear.
_BRIEF_ANCHOR_OK_RE = re.compile(r"\[brief-anchor-ok:\s*\S[^\]]*?\s*\]")

#: Scanned prefix for the opt-out token — a token buried deep in a long brief must
#: not silently authorise the whole prompt (the ``[quote-ok:]`` precedent).
_TOKEN_SCAN_CHARS = 512


def _gate_enabled() -> bool:
    from hooks.scripts.teatree_settings import teatree_bool_setting  # noqa: PLC0415 — deferred: cold-hook import

    return teatree_bool_setting("brief_anchor_gate_enabled", default=True)


def _refuse_mode() -> bool:
    from hooks.scripts.teatree_settings import teatree_bool_setting  # noqa: PLC0415 — deferred: cold-hook import

    return teatree_bool_setting("brief_anchor_gate_refuse", default=False)


def _prompt_of(data: dict) -> str:
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""
    prompt = tool_input.get("prompt", "")
    return prompt if isinstance(prompt, str) else ""


def _warning_for(prompt: str) -> str | None:
    """The lint warning for *prompt*, or ``None`` when it is anchored or asserts nothing."""
    src_dir = Path(__file__).resolve().parents[2] / "src"
    added = False
    try:
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
            added = True
        from teatree.hooks import brief_anchor_scanner  # noqa: PLC0415 — deferred: cold-hook import

        verdict = brief_anchor_scanner.find_unanchored_assertions(prompt)
        return None if verdict is None else brief_anchor_scanner.format_warning(verdict)
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(str(src_dir))


def handle_brief_anchor_lint(data: dict) -> bool:
    """Warn (or, in refuse mode, deny) a dispatch whose brief anchors none of its claims."""
    try:
        if data.get("tool_name") not in DISPATCH_TOOLS or not _gate_enabled():
            return False
        prompt = _prompt_of(data)
        if not prompt or _BRIEF_ANCHOR_OK_RE.search(prompt[:_TOKEN_SCAN_CHARS]):
            return False
        message = _warning_for(prompt)
        if message is None:
            return False
        if not _refuse_mode():
            sys.stderr.write(message + "\n")
            return False
    except Exception:  # noqa: BLE001 — crash-proof hook: a gate bug must never wedge the dispatch
        return False
    from hooks.scripts.hook_router import _fail_open_or_deny  # noqa: PLC0415 — deferred back-import: avoids a cycle

    return _fail_open_or_deny(data, message, gate_id="brief_anchor")
