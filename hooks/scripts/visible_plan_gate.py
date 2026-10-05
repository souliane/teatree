"""PreToolUse enforcement for an explicit multi-target plan-first contract.

Skills teach the default, but the owner can make ordering a binding runtime rule:
when the owner's own latest prompt names multiple tickets and orders a plan before
a change, the main agent must put a prospective plan for every target in visible
assistant text or a plan-bearing task before it can write, ask, or dispatch.
Reads stay open while the plan is prepared.

This is intentionally not a general plan detector.  It activates only on a
positive, narrow signal (two or more ticket IDs plus one sentence, not a question,
that orders a plan before a change), and it fails open when the transcript is
absent or unreadable.  The denial travels
through the router's shared safety spine, retaining self-rescue, the master
fail-open switch, and the deny circuit breaker.
"""

import re
import sys

from hooks.scripts.managed_repo import teatree_src_on_path
from hooks.scripts.orchestration_boundary_signals import call_is_from_subagent
from hooks.scripts.owner_prompts import entry_text, is_owner_prompt
from hooks.scripts.question_gates import _entry_message_role, read_transcript_entries
from hooks.scripts.skill_loader_input import strip_ambient_context
from hooks.scripts.teatree_settings import teatree_bool_setting

sys.modules.setdefault("visible_plan_gate", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.visible_plan_gate", sys.modules[__name__])

# Read, Grep and Glob stay open; a task is open only when it records the plan.
GATED_TOOLS = frozenset(
    {"Agent", "AskUserQuestion", "Bash", "Edit", "NotebookEdit", "TaskCreate", "TaskUpdate", "Write"}
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
# Only an ordered plan arms, so "report whether the plan before editing was approved" stays inert.
_PLAN_REQUIREMENT_RE = re.compile(
    r"(?:(?:^\W*|[:;,]\s*|\b(?:and|first|please|then)\s+)plan"
    r"|\b(?:draft|give|post|prepare|present|produce|provide|share|take|write)(?:\s+\S+){0,3}?\s+"
    r"(?:plan(?:s|ning)?|breakdown))\b.{0,320}"
    r"\bbefore\s+(?:(?:a|any|doing|i|making|the|we|you|your)\s+){0,2}"
    r"(?:act|chang|cod|commit|dispatch|edit|exec|fix|implement|merg|modif|patch|proceed|push|ship|start|touch|writ)",
    re.IGNORECASE,
)
_SENTENCE_BREAK_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
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


def _owner_turn(transcript_path: str) -> tuple[str, str]:
    """The owner's latest prompt, and visible text or complete task plans since it."""
    entries = read_transcript_entries(transcript_path)
    for index in range(len(entries) - 1, -1, -1):
        if is_owner_prompt(entries[index]):
            entry_body = entry_text(entries[index])
            command_args = _COMMAND_ARGS_RE.search(entry_body)
            if "<command-name>" in entry_body:
                if command_args is None:
                    continue
            elif any(marker in entry_body for marker in _HARNESS_WRAPPER_MARKERS):
                continue
            prompt = strip_ambient_context(command_args.group(1) if command_args else entry_body)
            targets = _explicit_plan_first_targets(prompt)
            since = (
                _assistant_plan_content(entry, targets)
                for entry in entries[index + 1 :]
                if _entry_message_role(entry) == "assistant"
            )
            return prompt, "\n".join(since)
    return "", ""


def _assistant_plan_content(entry: dict, targets: tuple[str, ...]) -> str:
    parts = [entry_text(entry)]
    message = entry.get("message")
    blocks = message.get("content", ()) if isinstance(message, dict) else ()
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if block.get("name") not in {"TaskCreate", "TaskUpdate"}:
                continue
            task = block.get("input")
            if isinstance(task, dict):
                task_plan = "\n".join(str(task.get(field, "")) for field in ("subject", "description"))
                if visible_plan_covers_targets(task_plan, targets):
                    parts.append(task_plan)
    return "\n".join(parts)


def _explicit_plan_first_targets(owner_text: str) -> tuple[str, ...]:
    """Ordered unique ticket IDs iff one non-question sentence orders a plan before a change."""
    sentences = (sentence.strip() for sentence in _SENTENCE_BREAK_RE.split(owner_text))
    if not any(_PLAN_REQUIREMENT_RE.search(sentence) for sentence in sentences if not sentence.endswith("?")):
        return ()
    found = (
        match.group(0) for match in _TICKET_ID_RE.finditer(owner_text) if match.group(1) not in _NON_TICKET_PREFIXES
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
        f"{joined}. The requested tool was not executed. First emit ordinary user-visible text or a plan-bearing "
        "TaskCreate/TaskUpdate with one target-labelled plan per ticket "
        "(implementation and test/verify, in either order). "
        "Reads stay open — Read, Grep, Glob and classified read-only Bash. A TaskCreate or TaskUpdate "
        "whose subject and description cover every target may record the plan. Only once both plans "
        "are visible or recorded may you ask a question, edit, or dispatch. "
        "Do not use placeholder Bash or printed tool syntax. "
        "A false positive escapes with `[visible-plan-ok: <reason>]` in the call, "
        "or `t3 <overlay> gate visible-plan disable`."
    )


def _unplanned_targets(transcript_path: str) -> tuple[str, ...]:
    """Targets the user bound to a plan that the current turn has not planned yet.

    Empty means allow — fewer than two targets, a visible or recorded plan, and any
    unreadable context all resolve the same way, which is this gate's fail-open.
    """
    if not transcript_path:
        return ()
    try:
        owner_prompt, written_since = _owner_turn(transcript_path)
        targets = _explicit_plan_first_targets(owner_prompt)
        covered = visible_plan_covers_targets(written_since, targets)
    except Exception:  # noqa: BLE001 -- cold hook is fail-open on unreadable context
        return ()
    return () if len(targets) < _MIN_TARGETS or covered else targets


def _is_read_only_bash(data: dict) -> bool:
    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if data.get("tool_name") != "Bash" or not isinstance(command, str):
        return False
    try:
        with teatree_src_on_path():
            from teatree.hooks.read_only_command import is_read_only  # noqa: PLC0415 -- deferred: cold-hook import

            return is_read_only(command) is True
    except Exception:  # noqa: BLE001 -- an unclassifiable command cannot be treated as read-only
        return False


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
    if not targets or _is_read_only_bash(data):
        return False
    if data.get("tool_name") in {"TaskCreate", "TaskUpdate"}:
        tool_input = data.get("tool_input")
        if isinstance(tool_input, dict):
            task_plan = "\n".join(str(tool_input.get(field, "")) for field in ("subject", "description"))
            if visible_plan_covers_targets(task_plan, targets):
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
