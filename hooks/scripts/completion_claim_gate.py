"""Stop: completion-claim gate (#2665).

The agent emits a completeness assertion — "done", "no blockers anywhere",
"ready to merge", "everything is here" — from the artifacts it produced
reviewing clean, NOT from every spec-defined deliverable verified on the actual
merge target. The representative failure: "no blockers anywhere" claimed on a
multi-deliverable ticket while the crucial deliverable was on the wrong surface
and its fix stranded off the merge target, surfaced only later under direct
review. A false completion claim that propagates downstream is a
highest-severity reliability gap.

This recurs despite the prose verification-before-completion rule and the
WARN-only closure-reverify advisory (#1448, which deliberately excludes "done").
Per the instruction-compliance escalation, a rule that recurs despite a memory
must become a non-bypassable GATE — so this Stop gate BLOCKS (the
structured-question gate's ``decision: block`` shape), refusing turn-end on a
HIGH-confidence multi-deliverable completion claim that lacks an on-target
deliverable->evidence map.

Precision posture (mirrors the structured-question gate, the only other
hard-blocking Stop gate): it fires ONLY on a loop-driven turn
(``_session_drives_loop`` — an attended interactive turn has a human reading the
prose, so the gate would be pointless nagging there), short-circuits the
``stop_hook_active`` re-fire, and the detector is tuned hard for precision so a
legitimate single-deliverable "done" or a complete on-target map is never
blocked. Two never-lockout escapes — a per-call ``[skip-completion-gate:
<reason>]`` token in the turn text and the ``[teatree]
completion_claim_gate_enabled = false`` kill-switch — keep a false fire from
ever wedging turn-end.

The detection lives in the pure ``teatree.hooks.completion_claim_scanner``
module; this handler is the thin transcript-reading wrapper, fail-safe-to-silent
on any error (a Stop hook must NEVER crash turn-end).
"""

import contextlib
import json
import re
import sys
from pathlib import Path

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# registers and a test patching a helper here operate on ONE module object.
sys.modules.setdefault("completion_claim_gate", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.completion_claim_gate", sys.modules[__name__])

_SKIP_COMPLETION_GATE_RE = re.compile(r"\[skip-completion-gate:\s*(\S[^\]]*?)\s*\]")


def _completion_claim_gate_enabled() -> bool:
    """Whether the completion-claim gate is enabled (default True).

    Fails OPEN to enabled on a missing/broken config; an explicit ``false``
    (``[teatree] completion_claim_gate_enabled = false``) is the one-line
    kill-switch (``t3 <overlay> gate completion-claim disable``). Reuses the
    shared bare-boolean reader so only a bare TOML ``false`` disables it.
    """
    from hooks.scripts.hook_router import _teatree_bool_setting  # noqa: PLC0415 deferred back-import

    return _teatree_bool_setting("completion_claim_gate_enabled", default=True)


def _skip_completion_gate_token(text: str) -> str | None:
    """Return the reason from a ``[skip-completion-gate: <reason>]`` token, else None.

    The Stop gate has no per-call tool input, so the escape lives in the
    assistant's final-turn text — the agent attaches the token when it has a
    legitimate reason the heuristic cannot see. An empty reason is rejected.
    """
    match = _SKIP_COMPLETION_GATE_RE.search(text)
    if match is None:
        return None
    reason = match.group(1).strip()
    return reason or None


def handle_completion_claim_gate(data: dict) -> bool | None:
    """Block a Stop whose final turn makes an unbacked multi-deliverable claim.

    Returns ``True`` (emitting a ``decision: block``) only when the last
    assistant turn carries a HIGH-confidence completeness assertion on a
    multi-deliverable ticket with no complete on-target deliverable->evidence
    map (the detector decides). Otherwise returns ``None`` so the session may
    end normally. Fail-safe-to-silent: any malformed input or unexpected error
    returns ``None`` so the Stop chain is never crashed.
    """
    src_dir = Path(__file__).resolve().parents[2] / "src"
    added = False
    try:
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
            added = True
        return _run_completion_claim_gate(data)
    except Exception:  # noqa: BLE001 — Stop hook must be crash-proof
        return None
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(str(src_dir))


def _gate_is_out_of_scope(data: dict) -> bool:
    """True when this Stop turn is exempt from the gate before reading the turn.

    The three skip conditions that need no transcript read: a ``stop_hook_active``
    re-fire (avoids a hot loop), an attended (non-loop-driven) turn a human reads,
    and the kill-switch being off. Folding them here keeps the main handler's
    return count within the lint budget without a suppression.
    """
    from hooks.scripts.hook_router import _session_drives_loop  # noqa: PLC0415 deferred back-import

    if data.get("stop_hook_active"):
        return True
    if not _session_drives_loop(data.get("session_id", "")):
        return True
    return not _completion_claim_gate_enabled()


def _run_completion_claim_gate(data: dict) -> bool | None:
    from hooks.scripts.hook_router import _last_assistant_turn  # noqa: PLC0415 deferred back-import
    from teatree.hooks import completion_claim_scanner  # noqa: PLC0415 — deferred: cold-hook import

    if _gate_is_out_of_scope(data):
        return None
    turn = _last_assistant_turn(data.get("transcript_path", ""))
    if turn is None:
        return None
    text = turn[0]
    if reason_token := _skip_completion_gate_token(text):
        sys.stderr.write(f"NOTE: completion-claim gate skipped via [skip-completion-gate: {reason_token}].\n")
        return None
    verdict = completion_claim_scanner.find_completion_block(text)
    if verdict is None:
        return None
    json.dump({"decision": "block", "reason": completion_claim_scanner.format_block_message(verdict)}, sys.stdout)
    return True
