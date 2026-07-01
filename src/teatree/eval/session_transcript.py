"""Pure parser for the ON-DISK Claude Code session JSONL.

This is a DIFFERENT schema from :mod:`teatree.eval.transcript`, which parses
the ``claude -p --output-format stream-json`` CLI stream. The on-disk session
log under ``~/.claude/projects/<slug>/<session-id>.jsonl`` carries a richer
envelope (parent/child uuids, sidechain marker, cwd, git branch) and folds hook
outcomes in as ``attachment`` events rather than as a separate stream. A reader
who assumes the two schemas are interchangeable will mis-extract; they are not.

Every line is one JSON envelope keyed by ``type``:

``assistant`` carries ``message.content[]`` blocks (``thinking`` / ``text`` /
``tool_use``); a ``tool_use`` block carries ``name``, ``input``, ``id`` and a
``caller`` object. A ``Skill`` tool call carries ``input.skill`` (e.g.
``t3:code``).

``user`` carries ``message.content`` as a str (a real prompt) or a list of
``tool_result`` blocks.

``attachment`` carries ``attachment.type`` discriminating the kind. Hook
outcomes use the version-volatile pair ``hook`` / ``hook_success`` (plus
``hook_blocking_error`` / ``hook_non_blocking_error`` / …) and carry
``hookEvent`` (PreToolUse / PostToolUse / TaskCreated / Stop / …), ``hookName``,
``exitCode`` (0 = allow, non-zero = deny), ``stdout`` / ``stderr``
(PRIVACY-SENSITIVE — never surfaced by the conformance report), ``toolUseID``
and ``command``.

Parsing is fail-soft: a malformed line, a missing field, or an unrecognised
hook discriminator yields a best-effort :class:`SessionEvent` (or is skipped)
rather than raising — the on-disk schema drifts between Claude Code versions.
"""

import dataclasses
import json
from typing import Any, cast

_HOOK_ATTACHMENT_TYPES: frozenset[str] = frozenset(
    {
        "hook",
        "hook_success",
        "hook_blocking_error",
        "hook_non_blocking_error",
        "hook_system_message",
        "hook_additional_context",
        "hook_cancelled",
        "async_hook_response",
    }
)


@dataclasses.dataclass(frozen=True)
class SessionEvent:
    """One ordered event from an on-disk session JSONL line.

    A single dataclass spans the three envelope kinds. ``tool_name`` /
    ``tool_input`` / ``skill`` are populated only for an ``assistant``
    ``tool_use`` block; ``hook_event`` / ``hook_exit_code`` / ``tool_use_id``
    only for a hook ``attachment``. ``raw`` keeps the parsed line so a caller
    can reach a field this dataclass does not surface.
    """

    line_no: int
    type: str
    is_sidechain: bool
    timestamp: str | None
    tool_name: str | None
    tool_input: dict[str, Any] | None
    skill: str | None
    hook_event: str | None
    hook_exit_code: int | None
    tool_use_id: str | None
    raw: dict[str, Any]


def _as_dict(value: object) -> dict[str, Any]:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _attachment_hook_fields(attachment: dict[str, Any]) -> tuple[str | None, int | None, str | None]:
    """Return ``(hook_event, exit_code, tool_use_id)`` from a hook attachment.

    Reads ``hookEvent`` / ``exitCode`` / ``toolUseID`` defensively — any may be
    absent on a given Claude Code version (the schema is volatile), in which
    case the corresponding slot is ``None`` and the event still parses.
    """
    return (
        _str_or_none(attachment.get("hookEvent")),
        _int_or_none(attachment.get("exitCode")),
        _str_or_none(attachment.get("toolUseID")),
    )


def _event_from_envelope(line_no: int, obj: dict[str, Any]) -> SessionEvent:
    event_type = obj.get("type")
    event_type = event_type if isinstance(event_type, str) else "unknown"
    attachment = _as_dict(obj.get("attachment"))
    is_hook = attachment.get("type") in _HOOK_ATTACHMENT_TYPES
    hook_event, exit_code, tool_use_id = _attachment_hook_fields(attachment) if is_hook else (None, None, None)
    return SessionEvent(
        line_no=line_no,
        type=event_type,
        is_sidechain=bool(obj.get("isSidechain")),
        timestamp=_str_or_none(obj.get("timestamp")),
        tool_name=None,
        tool_input=None,
        skill=None,
        hook_event=hook_event,
        hook_exit_code=exit_code,
        tool_use_id=tool_use_id,
        raw=obj,
    )


def _tool_use_event(line_no: int, envelope: SessionEvent, block: dict[str, Any]) -> SessionEvent | None:
    name = block.get("name")
    if not isinstance(name, str):
        return None
    tool_input = block.get("input")
    tool_input = dict(tool_input) if isinstance(tool_input, dict) else {}
    skill = _str_or_none(tool_input.get("skill")) if name == "Skill" else None
    return dataclasses.replace(
        envelope,
        line_no=line_no,
        tool_name=name,
        tool_input=tool_input,
        skill=skill,
        tool_use_id=_str_or_none(block.get("id")),
    )


def parse_session_jsonl(text: str) -> list[SessionEvent]:
    """Parse the on-disk session JSONL into ONE ordered event stream.

    An ``assistant`` line fans out into one :class:`SessionEvent` per
    ``tool_use`` block (so a turn issuing two tool calls yields two events);
    ``thinking`` / ``text`` blocks contribute a single envelope event carrying
    no tool. ``user`` and ``attachment`` lines each yield one envelope event.
    Order follows the file. Malformed lines are skipped.
    """
    events: list[SessionEvent] = []
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        envelope = _event_from_envelope(line_no, obj)
        if envelope.type != "assistant":
            events.append(envelope)
            continue
        content = _as_dict(obj.get("message")).get("content")
        tool_blocks = (
            [block for block in content if isinstance(block, dict) and block.get("type") == "tool_use"]
            if isinstance(content, list)
            else []
        )
        if not tool_blocks:
            events.append(envelope)
            continue
        events.extend(
            event for block in tool_blocks if (event := _tool_use_event(line_no, envelope, block)) is not None
        )
    return events


def extract_tool_calls(events: list[SessionEvent]) -> list[SessionEvent]:
    """Return only the events that carry a tool invocation (``tool_name`` set)."""
    return [event for event in events if event.tool_name is not None]


def extract_skill_invocations(events: list[SessionEvent]) -> list[SessionEvent]:
    """Return only the ``Skill`` tool calls (``skill`` set)."""
    return [event for event in events if event.skill is not None]


def extract_hook_events(events: list[SessionEvent]) -> list[SessionEvent]:
    """Return only the hook-attachment events (``hook_event`` set)."""
    return [event for event in events if event.hook_event is not None]
