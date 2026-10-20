"""Stop: slack-first gate — while factory Slack is live an owner prompt is relayed, not answered in the terminal.

Directive 5: an attended session hands the owner's prompt to the headless factory through
``t3 <overlay> pending-chat relay`` (or the MCP tool) and the terminal turn carries only the relay thread
link. The gate blocks a turn that answered or acted on a genuine owner prompt with no relay receipt or
refusal among the final turn's TOOL results — a marker typed into the agent's own prose does not count.

Django-free: liveness is the verdict the worker publishes (:mod:`teatree.standing_directives_cache`), so a
missing or stale verdict reads as not live and the terminal answer stands.

Never-lockout: ``stop_hook_active`` short-circuits the re-fire, the per-call ``[skip-slack-first: <reason>]``
token in the turn text clears one stop, the SDK lane, an unattended or child session and a session that is
not engaged are never gated, liveness is the off switch, and any internal error allows the stop.
"""

import contextlib
import json
import os
import re
import sys
from pathlib import Path

# Alias both identities so the live hook's bare import and a test's
# ``hooks.scripts.slack_first_gate`` import resolve ONE module object.
sys.modules.setdefault("slack_first_gate", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.slack_first_gate", sys.modules[__name__])

_SKIP_TOKEN_RE = re.compile(r"\[skip-slack-first:\s*(\S[^\]]*?)\s*\]")


def _skip_token(text: str) -> str | None:
    match = _SKIP_TOKEN_RE.search(text)
    return match.group(1).strip() if match and match.group(1).strip() else None


def _unattended() -> bool:
    return (
        os.environ.get("CLAUDE_CODE_SESSION_ATTENDED", "").strip() == "0"
        or os.environ.get("CLAUDE_CODE_CHILD_SESSION", "").strip() == "1"
    )


def handle_slack_first_gate(data: dict) -> bool | None:
    """Block a Stop whose final turn answered an owner prompt without relaying it while factory Slack is live.

    Returns ``True`` (emitting a ``decision: block``) only when the detector fires; otherwise ``None`` so the
    session may end normally. Fail-safe-to-silent on any error.
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
    from hooks.scripts.answer_first_gate import user_words_only  # noqa: PLC0415 deferred cold-hook import
    from hooks.scripts.owner_prompts import last_owner_turn  # noqa: PLC0415 deferred cold-hook import
    from hooks.scripts.question_gates import last_assistant_turn  # noqa: PLC0415 deferred cold-hook import
    from hooks.scripts.session_lane import LANE_SDK, session_lane  # noqa: PLC0415 deferred cold-hook import
    from hooks.scripts.standing_directives_delivery import engaged  # noqa: PLC0415 deferred cold-hook import
    from teatree import standing_directives_cache  # noqa: PLC0415 — deferred: cold-hook import
    from teatree.hooks import slack_first_scanner  # noqa: PLC0415 — deferred: cold-hook import

    session_id = str(data.get("session_id") or "")
    if data.get("stop_hook_active") or session_lane() == LANE_SDK or _unattended():
        return None
    if not session_id or not engaged(session_id):
        return None
    transcript_path = str(data.get("transcript_path") or "")
    turn = last_owner_turn(transcript_path)
    owner_words = user_words_only(turn.prompt)
    if not owner_words:
        return None
    if reason := _skip_token((last_assistant_turn(transcript_path) or ("", False))[0]):
        sys.stderr.write(f"NOTE: slack-first gate skipped via [skip-slack-first: {reason}].\n")
        return None
    published = standing_directives_cache.read_factory_slack()
    verdict = slack_first_scanner.find_unrelayed_prompt(
        owner_words, turn.tool_results, slack_live=published is not None and published["live"]
    )
    if verdict is None:
        return None
    json.dump({"decision": "block", "reason": slack_first_scanner.format_block_message(verdict)}, sys.stdout)
    return True
