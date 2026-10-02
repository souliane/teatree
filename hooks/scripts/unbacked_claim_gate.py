"""Stop: evidence gate — a diagnosis or an alarm cites what was read.

The completion-claim gate (#2665) demands per-deliverable evidence for a DONE
claim. This is the same principle one step earlier, at the DIAGNOSTIC claim: "CI
failed because X" needs the log line, and a severity escalation needs the artefact
that establishes it rather than a relayed symptom with the settling evidence
still outstanding. Both recorded failures were fluent, well-formed, and never
checked.

Like the answer-first sibling it does NOT skip an attended turn: an invented
diagnosis reported to a human is the failure being prevented, not a nag. The
precision lives in the detector — a low citation bar, an honest-hedge escape for
a diagnosis, and a label-shaped severity trigger scanned on fence-stripped text
so a quoted log level never fires it.

Never-lockout: ``stop_hook_active`` short-circuits the re-fire, the per-call
``[skip-evidence-gate: <reason>]`` token in the turn text clears one stop, the
``[teatree] unbacked_claim_gate_enabled = false`` kill-switch
(``t3 <overlay> gate unbacked-claim disable``) clears all of them, and any
internal error allows the stop — a Stop hook must never crash turn-end.
"""

import contextlib
import json
import re
import sys
from pathlib import Path

# Alias both identities so the live hook's bare import and a test's
# ``hooks.scripts.unbacked_claim_gate`` import resolve ONE module object.
sys.modules.setdefault("unbacked_claim_gate", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.unbacked_claim_gate", sys.modules[__name__])

_SKIP_TOKEN_RE = re.compile(r"\[skip-evidence-gate:\s*(\S[^\]]*?)\s*\]")


def _gate_enabled() -> bool:
    from hooks.scripts.teatree_settings import teatree_bool_setting  # noqa: PLC0415 deferred cold-hook import

    return teatree_bool_setting("unbacked_claim_gate_enabled", default=True)


def _skip_token(text: str) -> str | None:
    match = _SKIP_TOKEN_RE.search(text)
    if match is None:
        return None
    return match.group(1).strip() or None


def handle_unbacked_claim_gate(data: dict) -> bool | None:
    """Block a Stop whose final turn asserts a cause or an alarm with nothing cited.

    Returns ``True`` (emitting a ``decision: block``) only when the detector fires
    on the final assistant turn. Otherwise returns ``None`` so the session may end
    normally. Fail-safe-to-silent on any error.
    """
    src_dir = Path(__file__).resolve().parents[2] / "src"
    added = False
    try:
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
            added = True
        return _run(data)
    except Exception:  # noqa: BLE001 — Stop hook must be crash-proof
        return None
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(str(src_dir))


def _run(data: dict) -> bool | None:
    from hooks.scripts.hook_router import _last_assistant_turn  # noqa: PLC0415 deferred back-import
    from teatree.hooks import unbacked_claim_scanner  # noqa: PLC0415 — deferred: cold-hook import

    if data.get("stop_hook_active") or not _gate_enabled():
        return None
    turn = _last_assistant_turn(data.get("transcript_path", ""))
    if turn is None:
        return None
    text = turn[0]
    if reason := _skip_token(text):
        sys.stderr.write(f"NOTE: evidence gate skipped via [skip-evidence-gate: {reason}].\n")
        return None
    verdict = unbacked_claim_scanner.find_unbacked_claim(text)
    if verdict is None:
        return None
    json.dump({"decision": "block", "reason": unbacked_claim_scanner.format_block_message(verdict)}, sys.stdout)
    return True
