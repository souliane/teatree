"""Classifier-denial STOP gate (#1247) — a per-domain handler module of the router split.

Makes the "Classifier Denial Protocol" (skills/rules/SKILL.md) deterministic:
PostToolUse writes a per-session marker when a tool_response carries the canonical
denial preamble; the Stop gate turns a pending marker into a STOP-and-explain
systemMessage; the next UserPromptSubmit clears it. Fail-safe-to-empty: every
handler returns silently on malformed input so the hook never crashes the harness.
The state-dir helpers stay in the router and are back-imported lazily, so a test
patching router.STATE_DIR reaches the marker path.
"""

import json
import sys
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

_CLASSIFIER_DENIAL_PREAMBLE = "denied by the Claude Code auto mode classifier"
_CLASSIFIER_DENY_MARKER_SUFFIX = "classifier-deny"
_CLASSIFIER_DENY_ACTION_EXCERPT_MAX = 120

_DENIAL_RESPONSE_STRING_KEYS = ("error", "content", "stderr", "stdout", "message", "output", "reason")


def _tool_response_strings(tool_response: object) -> list[str]:
    """Return every string value reachable from ``tool_response`` (shallow).

    The classifier denial can land in ``error``, ``content``, ``stderr``,
    ``message``, ``output``, or as a bare string. We scan a fixed set of likely
    keys rather than recursing — keeps the detector cheap and predictable.
    Fail-safe-to-empty on unexpected shapes.
    """
    if isinstance(tool_response, str):
        return [tool_response]
    if not isinstance(tool_response, dict):
        return []
    response = cast("Mapping[str, object]", tool_response)
    out: list[str] = []
    for key in _DENIAL_RESPONSE_STRING_KEYS:
        value = response.get(key)
        if isinstance(value, str):
            out.append(value)
    return out


def _format_action_excerpt(tool_name: str, tool_input: object) -> str:
    """Build a short ``<tool_name>: <input>`` excerpt naming the denied action.

    Truncates to ``_CLASSIFIER_DENY_ACTION_EXCERPT_MAX`` characters so the Stop
    gate's systemMessage stays one line. Tries the common descriptive keys
    (``command``, ``file_path``, ``prompt``) before falling back to the repr of
    the full input.
    """
    name = tool_name if isinstance(tool_name, str) else "tool"
    excerpt = name
    if isinstance(tool_input, dict):
        input_dict = cast("Mapping[str, object]", tool_input)
        excerpt = f"{name}: {input_dict!r}"
        for key in ("command", "file_path", "prompt", "url", "channel"):
            value = input_dict.get(key)
            if isinstance(value, str) and value:
                excerpt = f"{name}: {value}"
                break
    if len(excerpt) > _CLASSIFIER_DENY_ACTION_EXCERPT_MAX:
        excerpt = excerpt[: _CLASSIFIER_DENY_ACTION_EXCERPT_MAX - 1] + "…"
    return excerpt


def handle_track_classifier_denial(data: dict) -> None:
    """PostToolUse: persist a marker when the classifier denies a tool call.

    Scans the ``tool_response`` payload for the canonical denial preamble and
    writes ``<session_id>.classifier-deny`` carrying enough context for the Stop
    gate to name what was denied. Returns silently on any missing/malformed
    field — fail-safe-to-empty per the spec.
    """
    from hooks.scripts.hook_router import _ensure_state_dir, _state_file  # noqa: PLC0415 deferred back-import

    if not isinstance(data, dict):
        return
    session_id = data.get("session_id", "")
    if not isinstance(session_id, str) or not session_id:
        return
    tool_response = data.get("tool_response")
    if tool_response is None:
        return
    strings = _tool_response_strings(tool_response)
    if not any(_CLASSIFIER_DENIAL_PREAMBLE in s for s in strings):
        return
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input")
    excerpt = _format_action_excerpt(tool_name, tool_input)
    payload = {
        "tool_name": tool_name if isinstance(tool_name, str) else "",
        "action": excerpt,
    }
    try:
        _ensure_state_dir()
        marker = _state_file(session_id, _CLASSIFIER_DENY_MARKER_SUFFIX)
        marker.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        # Fail-safe: a write failure must not crash the harness.
        return


def handle_classifier_deny_stop_gate(data: dict) -> bool | None:
    """Stop: emit STOP-and-explain ``systemMessage`` if a denial is pending.

    Returns ``True`` to break the Stop chain (mirrors the consideration gate
    pattern) when the marker exists. Otherwise returns ``None`` so the rest of
    the Stop chain runs unchanged.
    """
    from hooks.scripts.hook_router import _state_file  # noqa: PLC0415 deferred back-import

    if not isinstance(data, dict):
        return None
    session_id = data.get("session_id", "")
    if not isinstance(session_id, str) or not session_id:
        return None
    marker = _state_file(session_id, _CLASSIFIER_DENY_MARKER_SUFFIX)
    if not marker.is_file():
        return None
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    action = payload.get("action") or payload.get("tool_name") or "the denied tool call"
    body = (
        f"Classifier denied {action}. STOP and explain: action / reason / "
        'minimum-unblock — per the binding "Classifier Denial Protocol" '
        "(skills/rules/SKILL.md). Do not retry with a different argument "
        "shape, decompose the command, or switch tools. Ask the user via "
        'AskUserQuestion with two options: "Allow it (relax classifier)" '
        'or "Keep the denial (do it differently)".'
    )
    # Stop schema reserves ``hookSpecificOutput.additionalContext`` for other
    # events — emit the top-level ``systemMessage`` (schema-valid; non-decision;
    # visible to the agent) so the nag survives.
    json.dump({"systemMessage": body}, sys.stdout)
    return True


def handle_clear_classifier_deny_marker(data: dict) -> None:
    """UserPromptSubmit: clear the classifier-deny marker for this session.

    The next user turn re-arms the gate — the user either grants the per-call
    authorisation explicitly (which the agent now relays) or redirects to a
    different approach. Either way the previous denial is no longer the active
    blocker.
    """
    from hooks.scripts.hook_router import _state_file  # noqa: PLC0415 deferred back-import

    if not isinstance(data, dict):
        return
    session_id = data.get("session_id", "")
    if not isinstance(session_id, str) or not session_id:
        return
    marker = _state_file(session_id, _CLASSIFIER_DENY_MARKER_SUFFIX)
    try:
        marker.unlink(missing_ok=True)
    except OSError:
        return
