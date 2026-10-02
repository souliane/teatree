"""Sanctioned classifier-relax settings.json write gate (#807 companion, #857).

Allows an ``Edit``/``Write`` to ``~/.claude/settings.json`` — otherwise blocked
by the settings-write guard — ONLY when the transcript carries the sanctioned
Step-3 classifier-relax approval AND the write payload passes content-schema
validation (#857). Extracted whole from ``hook_router`` (the god-module is
shrink-only) so the router shrinks; it re-exports
:func:`handle_allow_classifier_relax_settings_write` into ``_HANDLERS`` plus the
detection primitives its tests read.

Threat model:

    WHAT THIS ALLOWS: an ``Edit``/``Write`` to ``~/.claude/settings.json`` only
    when there is transcript evidence of the exact Step-3 user approval from the
    sanctioned classifier-relax flow (a specific ``AskUserQuestion`` option text
    "Allow it (relax classifier)" AND an affirmative user response) AND the
    resulting payload is a schema-valid settings shape (#857).

    PER-WRITE / CONSUME-ONCE CONSENT: an approval authorises exactly ONE
    subsequent settings.json write — the next one — not every later write. The
    scan binds to the MOST RECENT approval pair and denies the replay once a
    settings.json write has occurred since that approval.

    CONTENT-SCHEMA VALIDATION (#857 — RESOLVED): the write payload is validated
    before the allow is emitted. A ``Write`` whose ``content`` is not a JSON
    object, whose ``permissions.{allow,deny,ask}`` / ``autoMode.allow`` are not
    lists of strings, or that adds a blanket-wildcard rule — a whole-tool grant
    with no scope for ANY built-in tool (``Bash``, ``Edit``, ``Write(*)``,
    ``Read(:*)``) — is REFUSED pre-persist; an ``Edit`` whose ``new_string`` adds a
    blanket-wildcard rule, or whose applied result parses to invalid JSON, is
    likewise refused. The gate only permits the SANCTIONED SHAPE the
    classifier-relax protocol produces — a smallest-scope string rule appended to
    an allow list.

    WHAT THIS DOES NOT ALLOW: any other target path; a write without transcript
    evidence of the approval; a replay of consumed consent; a payload that fails
    content-schema validation.

A schema-invalid write with a recorded approval is REFUSED through the router's
shared ``_fail_open_or_deny`` chokepoint (not a bare ``emit_pretooluse_deny``),
so the self-rescue allowlist and the master ``danger_gate_fail_open`` kill-switch
apply — a false positive in the validator can never wedge the classifier-relax
flow (the never-lockout contract, ``test_gate_never_lockout_contract``).

Cold-import safe: the live PreToolUse hook is a bare ``python3`` subprocess with
no guarantee ``teatree`` is importable, so the module top imports only stdlib
plus the ``question_gates`` sibling (the transcript-parsing home). The router
helpers ``_entry_role`` / ``_entry_content`` / ``_fail_open_or_deny`` stay in the
router (shared with other handlers) and are back-imported lazily.
"""

import json
import re
import sys
from pathlib import Path

from hooks.scripts.pretooluse_verdict import Verdict, emit_pretooluse_allow
from hooks.scripts.question_gates import read_transcript_entries as _read_transcript_entries

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# re-exports and a test patching a helper here operate on ONE module object.
sys.modules.setdefault("classifier_relax_gate", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.classifier_relax_gate", sys.modules[__name__])

_CLASSIFIER_RELAX_OPTION = "Allow it (relax classifier)"

# Affirmative selection of the relax option.  Precise, not loosely spoofable: it
# matches an explicit selection of the option label / "allow it" intent or a
# clear standalone yes — NOT a bare "relax" substring and NOT only a
# start-anchored "yes".  Excludes loose verbs like "do it" because the DECLINE
# option label is "Keep the denial (do it differently)".
_CLASSIFIER_RELAX_AFFIRMATIVE = re.compile(
    r"allow it(?:\s*\(relax classifier\))?"
    r"|relax classifier"
    r"|\byes\b"
    r"|\b(?:go ahead|approve|approved|affirmative|confirm|confirmed)\b",
    re.IGNORECASE,
)

# Single source of truth for the (unexpanded) settings path; expansion happens
# at call time (not import) so the test HOME override is respected.
_SETTINGS_JSON_PATH = "~/.claude/settings.json"

# Allow-list keys whose entries the sanctioned protocol appends to. Every entry
# must be a string; a non-string entry is a malformed relax write.
_ALLOW_LIST_KEYS = (("permissions", ("allow", "deny", "ask")), ("autoMode", ("allow",)))

# A blanket-wildcard permission rule that grants a whole tool with no scope —
# a bare tool (``Bash``, ``Edit``, ``Write``) or a wildcard/empty scope
# (``Bash()``, ``Edit(*)``, ``Write(* *)``, ``Read(:*)``). The rules protocol
# requires the SMALLEST rule that covers the use case, so a whole-tool grant of
# ANY built-in tool is never a sanctioned relax. Group 1 is the tool name.
_BLANKET_RULE_RE = re.compile(r"^([A-Za-z_]+)(?:\(\s*(?:\*(?:\s+\*)*|:\*|)\s*\)|)$")


def _settings_json_target() -> str:
    """Resolved absolute path of ``_SETTINGS_JSON_PATH`` (HOME-sensitive)."""
    return str(Path(_SETTINGS_JSON_PATH).expanduser())


def _block_is_settings_write(block: dict) -> bool:
    """True when ``block`` is an Edit/Write tool_use targeting settings.json."""
    if block.get("type") != "tool_use":
        return False
    if block.get("name") != "Edit" and block.get("name") != "Write":
        return False
    tool_input = block.get("input")
    raw_path = tool_input.get("file_path", "") if isinstance(tool_input, dict) else ""
    try:
        return str(Path(str(raw_path)).expanduser()) == _settings_json_target()
    except (OSError, ValueError, RuntimeError):
        return False


def _ask_question_has_relax_option(block: dict) -> bool:
    """True when an ``AskUserQuestion`` tool_use offers the verbatim relax option."""
    if block.get("type") != "tool_use" or block.get("name") != "AskUserQuestion":
        return False
    tool_input = block.get("input")
    questions = tool_input.get("questions", []) if isinstance(tool_input, dict) else []
    if not isinstance(questions, list):
        return False
    target = " ".join(_CLASSIFIER_RELAX_OPTION.split())
    for question in questions:
        if not isinstance(question, dict):
            continue
        options = question.get("options", [])
        if not isinstance(options, list):
            continue
        for option in options:
            label = option.get("label", option) if isinstance(option, dict) else option
            if isinstance(label, str) and " ".join(label.split()) == target:
                return True
    return False


def _user_entry_affirms_relax(entry: dict) -> bool:
    """True when a user transcript ``entry`` affirmatively selects the relax option."""
    from hooks.scripts.hook_router import _entry_content  # noqa: PLC0415 deferred back-import

    texts = [str(b.get("text", "")) for b in _entry_content(entry) if isinstance(b, dict) and b.get("type") == "text"]
    return bool(_CLASSIFIER_RELAX_AFFIRMATIVE.search(" ".join(texts).strip()))


def _denied_settings_write_ids(entries: list, start: int) -> set[str]:
    """Return the ``tool_use_id``s of settings writes DENIED since ``start``.

    A settings.json write that this gate denies leaves an ``is_error`` tool_result
    in the transcript; that attempt never landed. Its id is collected here so the
    consume-once scan does NOT treat it as spending the consent — the approval
    survives a denied attempt (a false-block, or any deny) so a corrected retry is
    still allowed (#3/#4). Fail-safe: an unparsable block is skipped.
    """
    from hooks.scripts.hook_router import _entry_content  # noqa: PLC0415 deferred back-import

    denied: set[str] = set()
    for k in range(start, len(entries)):
        for block in _entry_content(entries[k]):
            if not isinstance(block, dict) or block.get("type") != "tool_result" or not block.get("is_error"):
                continue
            tool_use_id = block.get("tool_use_id")
            if isinstance(tool_use_id, str):
                denied.add(tool_use_id)
    return denied


def _consent_unconsumed(entries: list, start: int) -> bool:
    """True when no LANDED settings write has spent the consent since ``start``.

    A settings write whose ``tool_use_id`` carries an is_error tool_result was
    denied — it did not land, so it does not consume the consent (the approval
    survives a denied attempt, #3/#4). Any other settings write since the approval
    is a completed write that spends it (a replay of consumed consent).
    """
    from hooks.scripts.hook_router import _entry_content  # noqa: PLC0415 deferred back-import

    denied_ids = _denied_settings_write_ids(entries, start)
    for k in range(start, len(entries)):
        for block in _entry_content(entries[k]):
            if isinstance(block, dict) and _block_is_settings_write(block) and block.get("id") not in denied_ids:
                return False
    return True


def _has_sanctioned_relax_approval(transcript_path: str) -> bool:
    """Return True only for an unconsumed, most-recent Step-3 relax approval.

    Walk the transcript from the END to the most-recent assistant
    ``AskUserQuestion`` offering the verbatim relax option; require the first
    subsequent user turn to affirmatively select it; then consume-once — a
    settings.json write that ACTUALLY LANDED since that approval spends the consent
    (return False, the pending write would be a replay). A write that was DENIED
    (its ``tool_use_id`` carries an ``is_error`` tool_result) did not land, so it
    does NOT consume the consent — the approval survives a denied attempt. Fail-safe
    to "no allow" on any missing/unmatched condition.
    """
    from hooks.scripts.hook_router import _entry_content, _entry_role  # noqa: PLC0415 deferred back-import

    entries = _read_transcript_entries(transcript_path)
    for idx in range(len(entries) - 1, -1, -1):
        entry = entries[idx]
        if _entry_role(entry) != "assistant":
            continue
        if not any(
            isinstance(block, dict) and _ask_question_has_relax_option(block) for block in _entry_content(entry)
        ):
            continue
        approval_user_idx: int | None = None
        for j in range(idx + 1, len(entries)):
            if _entry_role(entries[j]) != "user":
                continue
            if not _user_entry_affirms_relax(entries[j]):
                return False
            approval_user_idx = j
            break
        if approval_user_idx is None:
            return False
        return _consent_unconsumed(entries, approval_user_idx + 1)
    return False


def _is_blanket_rule(rule: str) -> bool:
    """Whether ``rule`` grants a whole tool with no scope (a blanket-wildcard rule).

    Any built-in tool granted bare (``Bash``, ``Edit``, ``Write``) or with a
    wildcard/empty scope (``Bash(*)``, ``Write(* *)``, ``Read(:*)``, ``Edit()``)
    is a blanket grant — the rules protocol requires the SMALLEST rule that covers
    the use case, so a whole-tool grant of ANY tool is never a sanctioned relax. An
    ``mcp__…`` tool name is its own finest grain (MCP tools carry no sub-scope), so
    a bare MCP grant is NOT a blanket rule.
    """
    match = _BLANKET_RULE_RE.match(rule.strip())
    return match is not None and not match.group(1).startswith("mcp__")


def _validate_allow_lists(payload: dict) -> str | None:
    """Return an error string if any allow list is malformed, else ``None``.

    Each configured allow list must be a list of strings, and no entry may be a
    blanket-wildcard rule. Absent keys are fine (a partial settings file).
    """
    for parent_key, list_keys in _ALLOW_LIST_KEYS:
        parent = payload.get(parent_key)
        if parent is None:
            continue
        if not isinstance(parent, dict):
            return f"`{parent_key}` must be a JSON object"
        for list_key in list_keys:
            entries = parent.get(list_key)
            if entries is None:
                continue
            if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
                return f"`{parent_key}.{list_key}` must be a list of strings"
            blanket = next((e for e in entries if _is_blanket_rule(e)), None)
            if blanket is not None:
                return (
                    f"blanket-wildcard rule `{blanket}` in `{parent_key}.{list_key}` — "
                    "use the smallest rule that covers the use case"
                )
    return None


def _validate_full_content(content: str) -> str | None:
    """Validate a full settings.json ``content`` string as a schema-valid object."""
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return "the write is not valid JSON"
    if not isinstance(payload, dict):
        return "the settings.json top level must be a JSON object"
    return _validate_allow_lists(payload)


def _new_string_adds_blanket_rule(new_string: str) -> str | None:
    """Return an error if an Edit ``new_string`` adds a blanket-wildcard rule.

    Only quoted tokens that are allow-list *entries* are candidate rules. A quoted
    token followed by a colon is a JSON KEY (``"allow":``, ``"permissions":``), not a
    permission rule, so it is skipped — otherwise the key ``allow`` matches the
    bare-tool blanket-rule shape and a legitimate Edit adding a scoped rule under an
    allow list is false-blocked (#4). Quotes are paired with ``finditer`` on the full
    ``"[^"]*"`` literal (a lookahead-``findall`` re-anchors on a key's closing quote
    and mis-pairs the rest, letting a real bare-tool entry slip past). This
    raw-fragment scan is only the FALLBACK; ``validate_relax_write`` prefers the
    parsed applied-content check, which validates real list entries and never keys.
    """
    for match in re.finditer(r'"([^"]*)"', new_string):
        if new_string[match.end() :].lstrip().startswith(":"):
            continue  # a JSON key, not a rule entry
        if _is_blanket_rule(match.group(1)):
            return f"blanket-wildcard rule `{match.group(1)}` — use the smallest rule that covers the use case"
    return None


def _applied_edit_content(tool_input: dict) -> str | None:
    """Return the settings.json content AFTER applying an Edit, or ``None`` if unresolvable.

    Reads the current file and applies the single ``old_string`` → ``new_string``
    substitution (all occurrences when ``replace_all``). Returns ``None`` — skip
    the full-JSON check, the fragment check still ran — when the file is
    unreadable or the ``old_string`` is not present (the Edit does not cleanly
    apply against what the gate can see).
    """
    try:
        current = Path(_settings_json_target()).read_text(encoding="utf-8")
    except OSError:
        return None
    old = str(tool_input.get("old_string", ""))
    new = str(tool_input.get("new_string", ""))
    if not old or old not in current:
        return None
    return current.replace(old, new) if tool_input.get("replace_all") else current.replace(old, new, 1)


def validate_relax_write(tool_name: str | None, tool_input: dict) -> str | None:
    """Return an error string if the relax settings write is malformed, else ``None`` (#857).

    A ``Write`` validates its full ``content``. An ``Edit`` prefers the parsed
    applied-content check when the current file is readable and the ``old_string``
    matches — ``_validate_full_content`` parses the resulting JSON and validates the
    real allow-list *entries* (never keys), so a legitimate rule added under an
    ``"allow":`` key is not false-blocked (#4). Only when the applied result is
    unresolvable (file unreadable / ``old_string`` absent) does it fall back to the
    raw ``new_string`` fragment scan, whose regex also excludes JSON keys. The user
    has approved THAT a write occurs; this check enforces the sanctioned SHAPE so a
    corrupting or over-broad write is refused pre-persist.
    """
    if tool_name == "Write":
        return _validate_full_content(str(tool_input.get("content", "")))
    applied = _applied_edit_content(tool_input)
    if applied is not None:
        return _validate_full_content(applied)
    return _new_string_adds_blanket_rule(str(tool_input.get("new_string", "")))


def handle_allow_classifier_relax_settings_write(data: dict) -> bool | Verdict | None:
    """Allow Edit/Write to ~/.claude/settings.json after sanctioned Step-3 approval.

    Emits the nested ``hookSpecificOutput`` allow envelope and returns the distinct
    :data:`Verdict.ALLOW` sentinel — which ``main()`` translates to exit 0 (the
    harness only honours a PreToolUse allow at exit 0 with the nested shape) — when
    the tool is ``Edit``/``Write``, the target resolves to ``~/.claude/settings.json``,
    the transcript carries an unconsumed Step-3 relax approval, AND the payload passes
    content-schema validation (#857). Returning ``True`` here instead (the pre-#3 bug)
    made ``main()`` exit 2, so the human approval acted as a BLOCK and burned the
    one-shot consent. A payload that fails validation with a recorded approval is
    REFUSED (the user approved that a write occurs, not a malformed one). Any other
    condition returns ``None`` — later handlers stay in play. Registered FIRST in the
    PreToolUse chain so it fires before the settings-write deny.
    """
    if data.get("tool_name") not in {"Edit", "Write"}:
        return None
    tool_input = data.get("tool_input") or {}
    raw_path = tool_input.get("file_path", "")
    if str(Path(str(raw_path)).expanduser()) != _settings_json_target():
        return None
    if not _has_sanctioned_relax_approval(data.get("transcript_path", "")):
        return None
    schema_error = validate_relax_write(data.get("tool_name"), tool_input)
    if schema_error is not None:
        from hooks.scripts.hook_router import _fail_open_or_deny  # noqa: PLC0415 deferred back-import

        return _fail_open_or_deny(
            data,
            "classifier-relax settings.json write failed content-schema validation (#857): "
            f"{schema_error}. Only a smallest-scope string rule may be appended to permissions.allow / "
            "autoMode.allow; no blanket wildcard, and the result must stay valid JSON.",
        )
    return emit_pretooluse_allow()
