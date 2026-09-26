"""PreToolUse enforcement for an explicit multi-target plan-first contract.

Skills teach the default, but a user can make ordering a binding runtime rule:
when the latest genuine user message names multiple tickets and explicitly asks
for a plan before action, the main agent must put a prospective plan for every
target in visible assistant text before it can act, ask, or dispatch.

This is intentionally not a general plan detector.  It activates only on a
positive, narrow signal (two or more ticket IDs plus plan-before wording), and
it fails open when the transcript is absent or unreadable.  The denial travels
through the router's shared safety spine, retaining self-rescue, the master
fail-open switch, and the deny circuit breaker.
"""

import re
import sys

from hooks.scripts.orchestration_boundary_signals import call_is_from_subagent
from hooks.scripts.question_gates import (
    _entry_message_blocks,
    _entry_message_role,
    is_tool_result_only,
    read_transcript_entries,
)
from hooks.scripts.teatree_settings import teatree_bool_setting
from hooks.scripts.turn_inspect import current_turn_assistant_text

sys.modules.setdefault("visible_plan_gate", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.visible_plan_gate", sys.modules[__name__])

# Read, Grep and Glob are absent: `skills/code/SKILL.md` exempts read-only
# investigation as part of planning, so gating them denies the very reading the
# plan is written from.
GATED_TOOLS = frozenset(
    {
        "Agent",
        "AskUserQuestion",
        "Bash",
        "Edit",
        "NotebookEdit",
        "TaskCreate",
        "TaskUpdate",
        "Write",
    }
)
_MIN_TARGETS = 2
_TOKEN_SCAN_LIMIT = 512
VISIBLE_PLAN_OK_RE = re.compile(r"\[visible-plan-ok:\s*(\S[^\]]*?)\s*\]")

_TICKET_ID_RE = re.compile(r"\b([A-Z]{2,10})-\d+\b")
# A standard, protocol, encoding, hash or model name is never a work item, and two
# of them beside the word "plan" armed the gate against every tool it governs.
_NON_TICKET_PREFIXES = frozenset(
    {
        "AES",
        "ANSI",
        "ASCII",
        "CVE",
        "CWE",
        "ECMA",
        "GPT",
        "HMAC",
        "HTTP",
        "HTTPS",
        "IEC",
        "IEEE",
        "IPV",
        "ISBN",
        "ISO",
        "MD",
        "OAUTH",
        "PBKDF",
        "PEP",
        "RFC",
        "RSA",
        "SAML",
        "SHA",
        "SSH",
        "TCP",
        "TLS",
        "UCS",
        "UDP",
        "UTC",
        "UTF",
    }
)
_PLAN_BEFORE_RE = re.compile(
    r"\b(?:plan(?:s|ning)?|breakdown)\b.{0,320}\bbefore\b",
    re.IGNORECASE | re.DOTALL,
)
_VISIBLE_PLAN_CUE_RE = re.compile(
    r"\b(?:plans?|breakdown|provisional|before (?:any )?(?:action|change|edit)|will|going to|intend to)\b",
    re.IGNORECASE,
)
_IMPLEMENT_RE = re.compile(
    r"\b(?:add|change|edit|fix|implement|remove|replace|refactor|update|write)\w*\b",
    re.IGNORECASE,
)
_VERIFY_RE = re.compile(
    r"\b(?:check|commit|push|run|ship|test|verif\w*)\b",
    re.IGNORECASE,
)
_CLAUSE_BREAK_RE = re.compile(r"[.;\n]")
# Skill bodies and slash-command expansions arrive as user entries; their
# worked examples are documentation, never the user's binding request.
_HARNESS_WRAPPER_MARKERS = ("<command-name>", "<skill-format>", "Base directory for this skill:")


def _gate_enabled() -> bool:
    """Whether the gate is enabled (default True; `t3 <overlay> gate visible-plan disable`)."""
    return teatree_bool_setting("visible_plan_gate_enabled", default=True)


def visible_plan_ok_token(data: dict) -> str | None:
    """The reason from a ``[visible-plan-ok: <reason>]`` token in the call, else None.

    Every string argument is scanned rather than a per-tool field list, because the
    gate governs tools whose only free text differs (Bash ``command``, Agent
    ``prompt``, Write ``content``). An empty reason does not unblock, and only the
    first 512 characters of each field count so a buried token cannot slip through.
    """
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    for value in tool_input.values():
        if not isinstance(value, str) or not value:
            continue
        match = VISIBLE_PLAN_OK_RE.search(value[:_TOKEN_SCAN_LIMIT])
        if match and (reason := match.group(1).strip()):
            return reason
    return None


def _latest_user_text(transcript_path: str) -> str:
    """Latest genuine user text, walking past tool-result pseudo-user entries."""
    for entry in reversed(read_transcript_entries(transcript_path)):
        if _entry_message_role(entry) != "user":
            continue
        blocks = _entry_message_blocks(entry)
        if is_tool_result_only(blocks):
            continue
        if entry.get("isMeta"):
            continue
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        text = (
            content
            if isinstance(content, str)
            else "\n".join(
                str(block.get("text", ""))
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "text"
            )
        )
        if any(marker in text for marker in _HARNESS_WRAPPER_MARKERS):
            continue
        return text
    return ""


def _explicit_plan_first_targets(user_text: str) -> tuple[str, ...]:
    """Ordered unique ticket IDs iff the user explicitly bound plan before action."""
    if not _PLAN_BEFORE_RE.search(user_text):
        return ()
    found = (
        match.group(0) for match in _TICKET_ID_RE.finditer(user_text) if match.group(1) not in _NON_TICKET_PREFIXES
    )
    return tuple(dict.fromkeys(found))


def _preceding_segment(text: str, start: int, other_re: re.Pattern[str]) -> str:
    """Text before a target mention, bounded by the last clause break or other target.

    The clause bound is what keeps one target's trailing actions from also reading
    as the next target's leading ones.
    """
    head = text[:start]
    others = tuple(other_re.finditer(head))
    floor = others[-1].end() if others else 0
    return head[max((break_.end() for break_ in _CLAUSE_BREAK_RE.finditer(head, floor)), default=floor) :]


def _target_segments(text: str, target: str, all_targets: tuple[str, ...]) -> tuple[str, ...]:
    """Every window a target's own actions may occupy — after its mention, and before it.

    A plan labels its actions with the id on either side of them, so both sides count.
    """
    matches = tuple(re.finditer(rf"\b{re.escape(target)}\b", text, re.IGNORECASE))
    other_re = re.compile(
        "|".join(rf"\b{re.escape(other)}\b" for other in all_targets if other != target),
        re.IGNORECASE,
    )
    segments: list[str] = []
    for match in matches:
        next_other = other_re.search(text, match.end())
        end = next_other.start() if next_other else min(len(text), match.end() + 1200)
        segments.extend((text[match.end() : end], _preceding_segment(text, match.start(), other_re)))
    return tuple(segments)


def visible_plan_covers_targets(text: str, targets: tuple[str, ...]) -> bool:
    """True when every target owns both an implementation and a verification action.

    Order is free: `skills/code/SKILL.md` mandates the failing test before the
    implementation, so demanding verification LAST denied every TDD plan.
    """
    if len(targets) < _MIN_TARGETS or not _VISIBLE_PLAN_CUE_RE.search(text):
        return False
    return all(
        any(
            _IMPLEMENT_RE.search(segment) and _VERIFY_RE.search(segment)
            for segment in _target_segments(text, target, targets)
        )
        for target in targets
    )


def _deny_reason(targets: tuple[str, ...]) -> str:
    joined = ", ".join(targets)
    return (
        "TEATREE PLAN-FIRST GATE — the user explicitly required a visible per-target plan before action, "
        "but the current assistant turn does not yet contain an implementation→test/verify sequence for each of: "
        f"{joined}. The requested tool was not executed. First emit ordinary user-visible text with one "
        "target-labelled provisional plan block per ticket (implementation and test/verify/ship, in either order). "
        "Reading, grepping and globbing stay open — they are how the plan gets written. Only once both plans are "
        "visible may you ask a question, edit, or dispatch. Do not use placeholder Bash or printed tool syntax. "
        "A false positive escapes with `[visible-plan-ok: <reason>]` in the call, "
        "or `t3 <overlay> gate visible-plan disable`."
    )


def _unplanned_targets(transcript_path: str) -> tuple[str, ...]:
    """Targets the user bound to a plan that the current turn has not planned yet.

    Empty means allow — fewer than two targets, an already-visible plan, and any
    unreadable context all resolve the same way, which is this gate's fail-open.
    """
    if not transcript_path:
        return ()
    try:
        targets = _explicit_plan_first_targets(_latest_user_text(transcript_path))
        covered = visible_plan_covers_targets(current_turn_assistant_text(transcript_path), targets)
    except Exception:  # noqa: BLE001 -- cold hook is fail-open on unreadable context
        return ()
    return () if len(targets) < _MIN_TARGETS or covered else targets


def handle_enforce_visible_plan_before_tools(data: dict) -> bool:
    """Deny a governed main-agent tool until the explicit plan contract is met.

    **Never-lockout escapes**, mandated by ``hooks/CLAUDE.md`` because the gate
    governs tools an agent needs to rescue itself with:

    1. Per-call token ``[visible-plan-ok: <non-empty-reason>]`` in any string
        argument (first 512 chars).
    2. Config kill-switch ``visible_plan_gate_enabled = false``
        (``t3 <overlay> gate visible-plan disable``, itself on the self-rescue
        allowlist so this gate can never deny its own disable).
    3. ``_fail_open_or_deny`` — the self-rescue allowlist plus the master
        ``danger_gate_fail_open`` switch — and the deny-circuit breaker, whose
        UX allow-list carries this gate's prefix so a retry loop fails open
        rather than escalating.
    """
    if data.get("tool_name") not in GATED_TOOLS or call_is_from_subagent(data) or not _gate_enabled():
        return False
    targets = _unplanned_targets(str(data.get("transcript_path", "")))
    if not targets:
        return False
    if reason_token := visible_plan_ok_token(data):
        sys.stderr.write(f"NOTE: visible-plan gate skipped via [visible-plan-ok: {reason_token}].\n")
        return False

    from hooks.scripts.hook_router import _fail_open_or_deny  # noqa: PLC0415 -- deferred cycle

    return _fail_open_or_deny(data, _deny_reason(targets), gate_id="visible_plan_gate")


__all__ = [
    "GATED_TOOLS",
    "handle_enforce_visible_plan_before_tools",
    "visible_plan_covers_targets",
    "visible_plan_ok_token",
]
