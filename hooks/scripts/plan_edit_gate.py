"""Plan-before-code edit/Bash gate helpers (#2425).

Cohesive logic for the plan-gate's gated-set decision, factored out of the
shrink-only ``hook_router`` god-module: the change-vs-read Bash classifier, the
gated-tool predicate, and the per-call ``[skip-plan-gate: <reason>]`` escape
scanner. ``hook_router.handle_block_edit_before_planned`` imports these; this
module owns no I/O and no Django, so it stays a fast pure-logic unit.

The gate blocks a change attempt while the cwd worktree ticket is still in the
``STARTED`` FSM state (no ``PlanArtifact`` yet). The gated set is ``Edit`` /
``Write`` (any file change) AND ``Bash`` matching a change-making verb; read-only
investigation (``git status`` / ``log`` / ``diff`` / ``show``, API reads) is
allow-by-default and never matched — plans are for changes, not for looking.
"""

import re

# Per-call escape for the plan-edit gate: ``[skip-plan-gate: <non-empty-reason>]``
# in the current Edit/Write/Bash tool call's new_string/content/file_path/command
# unblocks that single call. Mirrors ``_SKILL_LOAD_OK_RE`` / ``_SKIP_SKILL_GATE_RE``
# in shape and 512-char truncation scope — buried tokens do not silently escape.
SKIP_PLAN_GATE_RE: re.Pattern[str] = re.compile(r"\[skip-plan-gate:\s*(\S[^\]]*?)\s*\]")

# A Bash command MAKES A CHANGE (vs. reads) when its leading verb — after benign
# ``cd``/env prefixes are not in scope here — is a git write or a PR write. The
# plan-gate blocks these before a plan exists; read-only investigation
# (``git status``/``log``/``diff``/``show``, API reads) is allow-by-default and
# never matched, so looking is always free (#2425 "plans are for changes, not for
# looking"). Anchored on the command verb at a segment boundary so the phrase
# embedded in a commit-message body or a doc string does not false-fire.
CHANGE_MAKING_BASH_RE: re.Pattern[str] = re.compile(
    r"(?:^|[;&|]\s*|&&\s*|\|\|\s*)(?:"
    r"git\s+(?:commit|push|merge|rebase|cherry-pick|am)\b|"
    r"gh\s+pr\s+(?:create|merge)\b|"
    r"glab\s+mr\s+(?:create|merge)\b"
    r")"
)

# The first 512 chars of each scanned field are searched for the escape token —
# matching the skill-loading gate so a buried token in a long body does not
# silently authorise the call.
_TOKEN_SCAN_LIMIT = 512
_SKIP_TOKEN_FIELDS = ("command", "new_string", "content", "file_path")


def is_change_making_bash(command: str) -> bool:
    """Whether *command* makes a change the plan-gate must block before PLANNED.

    True for a git write (commit/push/merge/rebase/cherry-pick/am) or a PR/MR
    write (``gh pr create|merge`` / ``glab mr create|merge``); False for every
    read-only investigation command, so the gate never blocks looking. Matched on
    the leading verb at a command-segment boundary (mirrors ``_PYTHON_TOOL_RE``).
    """
    return bool(CHANGE_MAKING_BASH_RE.search(command))


def plan_gate_applies_to_tool(data: dict) -> bool:
    """Whether this tool call is in the plan-gate's gated set.

    ``Edit``/``Write`` always qualify (any file change). ``Bash`` qualifies only
    when its command makes a change (:func:`is_change_making_bash`); a read-only
    Bash command is never gated — investigation stays free. Every other tool is
    out of scope.
    """
    tool_name = data.get("tool_name", "")
    if tool_name in {"Edit", "Write"}:
        return True
    if tool_name != "Bash":
        return False
    tool_input = data.get("tool_input")
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    return isinstance(command, str) and is_change_making_bash(command)


def skip_plan_gate_token(data: dict) -> str | None:
    """Return the reason from a ``[skip-plan-gate: <reason>]`` token, else None.

    Scans the current tool call's text within the first 512 characters of each
    field — for ``Edit``/``Write`` the ``new_string``/``content``/``file_path``;
    for ``Bash`` the ``command`` (the change-making-Bash arm's per-call escape) —
    so a buried token in a long body does not silently authorise the call. An
    empty reason returns None.
    """
    tool_input = data.get("tool_input", {})
    if not isinstance(tool_input, dict):
        return None
    for field in _SKIP_TOKEN_FIELDS:
        value = tool_input.get(field, "")
        if not isinstance(value, str) or not value:
            continue
        match = SKIP_PLAN_GATE_RE.search(value[:_TOKEN_SCAN_LIMIT])
        if not match:
            continue
        reason = match.group(1).strip()
        if reason:
            return reason
    return None
