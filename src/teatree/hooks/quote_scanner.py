"""Pre-publish quote-scanner gate (#1213).

A leak-prevention scanner that intercepts every tool call about to
publish text to an external surface (a public-repo issue/PR body, a
commit message, a Slack message, a t3 review post) and refuses the call
when the body matches user-attributed quote patterns.

The rule has been prose only in the memory ledger and recurred multiple
times in a single session before being promoted to a tooling gate per
``feedback_failed_memory_escalate_to_enforcement.md``.

Design notes:

The module is pure detection. The Bash/t3 command surfaces are parsed
into a payload, then the payload runs through :func:`scan_text`. The
PreToolUse hook in ``hooks/scripts/hook_router.py`` is the only place
that knows about ``stdout``/``permissionDecision`` JSON.

Patterns are split into ``HIGH`` (refuse publish) and ``MEDIUM`` (warn
but allow). Both severities log to a JSONL ledger so cold review can
reconstruct what the gate saw.

The blocklist file at ``$T3_DATA_DIR/quote-blocklist.txt`` (under the
XDG data dir by default) is a *regex* list, not a quote
archive. Each non-blank, non-``#``-prefixed line is compiled with
``re.IGNORECASE``. The spec is explicit: blocklists must not embed
the raw quotes they protect against.

Override via ``--quote-ok`` flag in the command string or
``QUOTE_OK=1`` in the tool-input env mapping. Either one bypasses all
checks and is itself logged for audit.
"""

import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, TypedDict

from teatree.hooks._command_parser import FAIL_CLOSED_SENTINEL as _FAIL_CLOSED_SENTINEL
from teatree.hooks._command_parser import extract_bash_payload as _extract_bash_payload
from teatree.hooks._command_parser import is_fail_closed_sentinel as _is_fail_closed_sentinel
from teatree.hooks._command_parser import is_publish_command as _is_publish_command
from teatree.hooks._hook_state import hook_state_root, note_env_override_once
from teatree.hooks._publish_detection import segment_word_lists_raw as _segment_word_lists_raw

_QUOTE_OK_ENV = "QUOTE_OK"

Severity = str  # "high" | "medium"


class ToolInput(TypedDict, total=False):
    """Subset of the PreToolUse ``tool_input`` payload this gate reads.

    Only the keys the scanner inspects are typed here — the harness
    actually passes a wider, tool-shaped mapping that we treat as opaque.
    """

    command: str
    text: str
    message: str
    body: str
    env: dict[str, str]
    # Agent/Task dispatch fields scanned by the pre-dispatch gate (#1401).
    prompt: str
    description: str


HIGH: Final[Severity] = "high"
MEDIUM: Final[Severity] = "medium"

# Name of the HIGH finding :func:`scan_text` injects when the parser could not
# resolve a body source (an unreadable file, a ``$VAR`` / stdin body). It is a
# fail-closed marker, NOT a real verbatim-quote content match — the gate never
# actually saw a user quote, only that the body was unreadable.
FAIL_CLOSED_FINDING_NAME: Final[str] = "fail-closed-sentinel"


@dataclass(frozen=True)
class Pattern:
    """A named regex with a severity classification."""

    name: str
    severity: Severity
    regex: re.Pattern[str]


@dataclass(frozen=True)
class Finding:
    """One pattern match against a body."""

    name: str
    severity: Severity
    excerpt: str


@dataclass
class ScanResult:
    """Aggregated severities + the matches that produced them."""

    findings: list[Finding] = field(default_factory=list)

    @property
    def has_high(self) -> bool:
        return any(f.severity == HIGH for f in self.findings)

    @property
    def has_medium(self) -> bool:
        return any(f.severity == MEDIUM for f in self.findings)

    @property
    def high(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == HIGH]

    @property
    def medium(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == MEDIUM]


# ── Detection patterns ──────────────────────────────────────────────

# HIGH: heading shapes that announce a verbatim user block.
_HEADING_PATTERNS: Final[tuple[Pattern, ...]] = (
    Pattern(
        "heading-user-mandate",
        HIGH,
        re.compile(
            r"^#{1,3}\s*User\s+(?:mandate|ask|directive|motivation|redirect|feedback)\b",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    Pattern(
        "heading-user-ask-verbatim",
        HIGH,
        re.compile(r"^#{1,3}\s*User\s+ask\s*\(verbatim", re.IGNORECASE | re.MULTILINE),
    ),
    Pattern(
        "bold-user-directive-verbatim",
        HIGH,
        re.compile(r"\*\*User\s+(?:directive|ask|mandate)[^*]*\(verbatim", re.IGNORECASE),
    ),
)

# HIGH: verbatim quote shapes — attributed quotation, italicised speech,
# explicit "the user said" prose.
_VERBATIM_PATTERNS: Final[tuple[Pattern, ...]] = (
    Pattern(
        "blockquote-attributed",
        HIGH,
        re.compile(r"^>\s*\"[A-Z]", re.MULTILINE),
    ),
    Pattern(
        "italic-quote-long",
        HIGH,
        re.compile(r'_"[^"]{20,}"_'),
    ),
    Pattern(
        "per-user-feedback-quoted",
        HIGH,
        re.compile(r"per\s+user\s+feedback\s+\"", re.IGNORECASE),
    ),
    Pattern(
        "the-user-said-colon",
        HIGH,
        re.compile(r"\bthe\s+user\s+(?:said|asked|wants?)\s*:", re.IGNORECASE),
    ),
)

# MEDIUM: attribution shapes that don't necessarily include a verbatim
# quote but lean on the user as the source of authority. Allowed past
# the gate (with a stderr warning) so a cold reviewer can verify the
# tone is paraphrased and not lifted.
_ATTRIBUTION_PATTERNS: Final[tuple[Pattern, ...]] = (
    Pattern(
        "per-user-direction",
        MEDIUM,
        re.compile(r"\bper\s+user\s+(?:direction|mandate|ask)\b", re.IGNORECASE),
    ),
    Pattern(
        "red-card-from-user",
        MEDIUM,
        re.compile(r"\bRED\s*CARD\b[^.\n]*\bfrom\s+user\b", re.IGNORECASE),
    ),
    Pattern(
        "the-user-has-verb",
        MEDIUM,
        re.compile(r"\bthe\s+user\s+has\s+(?:explicitly|mandated|directed)\b", re.IGNORECASE),
    ),
)

_BUILTIN_PATTERNS: Final[tuple[Pattern, ...]] = (
    *_HEADING_PATTERNS,
    *_VERBATIM_PATTERNS,
    *_ATTRIBUTION_PATTERNS,
)


def _blocklist_path() -> Path:
    return hook_state_root() / "quote-blocklist.txt"


# Compiled-blocklist cache keyed by resolved path -> (mtime_ns, size, patterns).
# The file is user-editable and rarely changes, so a single compile per (mtime,
# size) generation avoids re-parsing every ``scan_text`` call.
_BLOCKLIST_CACHE: dict[Path, tuple[int, int, list[Pattern]]] = {}


def _compile_blocklist(target: Path) -> list[Pattern]:
    """Compile every valid regex line, SKIPPING (never raising on) a bad one.

    A single malformed line must never disable the whole gate: the sole caller
    (``hook_router.handle_quote_scanner_pretool``) swallows exceptions and fails
    OPEN, so a raised ``re.error`` would turn the entire #1213 leak gate into a
    silent no-op on every publish. Each bad line is skipped with a one-line
    stderr NOTE naming the file+line so a typo is visible, not silent.
    """
    patterns: list[Pattern] = []
    for idx, raw in enumerate(target.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            compiled = re.compile(line, re.IGNORECASE)
        except re.error as exc:
            sys.stderr.write(f"NOTE: quote-scanner skipped invalid blocklist regex in {target}:{idx} — {exc}\n")
            continue
        patterns.append(Pattern(name=f"blocklist:{idx}", severity=HIGH, regex=compiled))
    return patterns


def _load_blocklist_patterns(path: Path | None = None) -> list[Pattern]:
    """Return the compiled blocklist patterns, mtime-cached per resolved path.

    The file is a list of REGEX patterns (one per line) — never raw quotes.
    Blank lines and ``#``-prefixed comments are skipped; each remaining line is
    compiled with ``re.IGNORECASE``. A line that fails to compile is skipped
    (:func:`_compile_blocklist`) rather than raised, so one bad regex never
    fails the gate open. The result is cached by ``(mtime_ns, size)`` so an
    unchanged file is compiled once, not on every scan.
    """
    target = path if path is not None else _blocklist_path()
    if not target.is_file():
        return []
    try:
        stat = target.stat()
    except OSError:
        return []
    cached = _BLOCKLIST_CACHE.get(target)
    if cached is not None and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        return cached[2]
    patterns = _compile_blocklist(target)
    _BLOCKLIST_CACHE[target] = (stat.st_mtime_ns, stat.st_size, patterns)
    return patterns


# Unicode smart-quote variants normalised to their ASCII equivalents before
# pattern matching. Codex round-2 #7 surfaced curly-quoted blockquote bodies
# bypassing every quote-aware regex — the fix is upstream normalisation, not
# new patterns per quote shape. Code points referenced by ``\N{...}`` so the
# lint checker is not confused by ambiguous glyphs in the source file.
_SMART_QUOTE_TRANSLATIONS: Final[dict[int, str]] = {
    # Double quotes
    ord("\N{LEFT DOUBLE QUOTATION MARK}"): '"',
    ord("\N{RIGHT DOUBLE QUOTATION MARK}"): '"',
    ord("\N{DOUBLE LOW-9 QUOTATION MARK}"): '"',
    ord("\N{DOUBLE HIGH-REVERSED-9 QUOTATION MARK}"): '"',
    ord("\N{LEFT-POINTING DOUBLE ANGLE QUOTATION MARK}"): '"',
    ord("\N{RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK}"): '"',
    # Single quotes / apostrophes
    ord("\N{LEFT SINGLE QUOTATION MARK}"): "'",
    ord("\N{RIGHT SINGLE QUOTATION MARK}"): "'",
    ord("\N{SINGLE LOW-9 QUOTATION MARK}"): "'",
    ord("\N{SINGLE HIGH-REVERSED-9 QUOTATION MARK}"): "'",
}


def _normalize_quotes(text: str) -> str:
    """Translate Unicode smart-quote variants to straight ASCII quotes.

    The detection regexes are written against ASCII quotes; normalising
    upstream means a single regex per shape continues to cover every
    typographic variant a publish surface might emit.
    """
    return text.translate(_SMART_QUOTE_TRANSLATIONS)


# ── HIGH shape patterns that require adjacent quote evidence (#3240) ──

# The two attribution SHAPES that fire on agent paraphrase as readily as on a
# verbatim quote: ``## User mandate`` / ``the user said:`` announce that a user
# spoke, but say nothing about whether what follows is word-for-word. They stay
# HIGH only when a verbatim quotation of meaningful length sits in the adjacent
# paragraph; otherwise they downgrade to MEDIUM warn-allow like the rest of
# ``_ATTRIBUTION_PATTERNS`` (a motivation heading or a requirement summary is
# author-voice paraphrase, not the verbatim leak #1213 blocks). The explicit
# ``(verbatim`` heading/bold patterns and the real quote shapes
# (``blockquote-attributed``/``italic-quote-long``/blocklist/fail-closed
# sentinel) keep HIGH unconditionally — they structurally announce word-for-word
# content.
_QUOTE_EVIDENCE_REQUIRED: Final[frozenset[str]] = frozenset({"the-user-said-colon", "heading-user-mandate"})

# A double-quoted span of 20+ chars — the same length threshold
# ``italic-quote-long`` uses to distinguish a real verbatim block from an
# incidental short quote.
_LONG_QUOTE_RE: Final[re.Pattern[str]] = re.compile(r'"[^"]{20,}"')


def _adjacent_region(text: str, match: re.Match[str]) -> str:
    r"""Return the match's own paragraph plus the paragraph immediately after it.

    Paragraphs are blank-line (``\n\n``) separated. A ``the user said:`` colon
    attribution carries its quote on the same line (its own paragraph); a
    ``## User mandate`` heading carries the verbatim block in the section right
    below it (the next paragraph) — so the adjacency window spans both. A quote
    two or more paragraphs away is NOT adjacent and does not keep the shape HIGH.
    """
    para_start = text.rfind("\n\n", 0, match.start())
    start = 0 if para_start == -1 else para_start + 2
    first_break = text.find("\n\n", match.end())
    if first_break == -1:
        return text[start:]
    second_break = text.find("\n\n", first_break + 2)
    return text[start:] if second_break == -1 else text[start:second_break]


def _resolved_severity(pattern: Pattern, text: str, match: re.Match[str]) -> Severity:
    """Return ``pattern``'s severity for ``match``, downgrading a shape-only attribution.

    A pattern in :data:`_QUOTE_EVIDENCE_REQUIRED` stays HIGH only when a 20+-char
    double-quoted span sits in the adjacent paragraph (:func:`_adjacent_region`);
    absent that evidence it downgrades to :data:`MEDIUM` (#3240). Every other
    pattern keeps its declared severity.
    """
    if pattern.name in _QUOTE_EVIDENCE_REQUIRED and _LONG_QUOTE_RE.search(_adjacent_region(text, match)) is None:
        return MEDIUM
    return pattern.severity


def scan_text(text: str, *, blocklist_path: Path | None = None) -> ScanResult:
    """Match every built-in pattern and every blocklist regex against ``text``.

    Smart-quote variants in the input are normalised to ASCII before
    matching so a single regex catches all typographic forms.

    A body the parser could not resolve carries the fail-closed sentinel
    (``FAIL_CLOSED_SENTINEL``) as its own discrete payload line. That line
    is recognised EXPLICITLY as a HIGH finding rather than relying on it
    tripping a content pattern — the old wording self-matched
    ``the-user-said-colon`` and produced a bogus user-quote finding on a
    body the scanner never saw (#126). Only the standalone sentinel LINES
    are excised before content matching, so a prose body fragment that
    merely names the sentinel on a different line is still scanned, while
    the marker text itself can never produce a second content-shaped
    finding. Inert prose that names the sentinel mid-line never trips the
    fail-closed branch at all (#1213).
    """
    result = ScanResult()
    if not text:
        return result
    if _is_fail_closed_sentinel(text):
        result.findings.append(Finding(name=FAIL_CLOSED_FINDING_NAME, severity=HIGH, excerpt="unresolved body source"))
        text = "\n".join(line for line in text.split("\n") if line.strip() != _FAIL_CLOSED_SENTINEL)
        if not text.strip():
            return result
    normalized = _normalize_quotes(text)
    for pattern in (*_BUILTIN_PATTERNS, *_load_blocklist_patterns(blocklist_path)):
        match = pattern.regex.search(normalized)
        if match is None:
            continue
        excerpt = match.group(0)[:120]
        severity = _resolved_severity(pattern, normalized, match)
        result.findings.append(Finding(name=pattern.name, severity=severity, excerpt=excerpt))
    return result


# ── Slack MCP write-tool body-field allowlist ──────────────────────

# Slack MCP outbound write tools → body-field map. The substring "send"
# heuristic from round 1 missed ``slack_schedule_message``,
# ``slack_create_canvas`` and ``slack_update_canvas`` — explicit allowlist
# replaces it (codex round-2 #6).
_SLACK_MCP_WRITE_TOOLS: Final[dict[str, tuple[str, ...]]] = {
    "slack_send_message": ("text", "message"),
    "slack_send_message_draft": ("text", "message"),
    "slack_schedule_message": ("text", "message"),
    "slack_edit_message": ("text", "message"),
    "slack_create_canvas": ("document_content", "content", "text"),
    "slack_update_canvas": ("document_content", "content", "text"),
    "slack_create_conversation": ("name",),
}


def _slack_tool_suffix(tool_name: str) -> str:
    """Extract the trailing slack tool name (``slack_*``) from an MCP id.

    MCP tool ids are shaped ``mcp__<server>__<tool>`` — we want the
    ``<tool>`` segment for an exact-match allowlist lookup.
    """
    return tool_name.rsplit("__", 1)[-1]


def _extract_slack_mcp_payload(tool_name: str, tool_input: ToolInput) -> str | None:
    """Return body text for a Slack MCP write tool, or ``None`` for read-only tools.

    The Slack MCP carries the body in tool-specific fields beyond what
    :class:`ToolInput` enumerates (``document_content``, ``content``,
    ``name``). The actual hook payload is a wider mapping than the
    typed view — we read each candidate field by name via :meth:`get`.
    """
    if not (tool_name.startswith("mcp__") and "slack" in tool_name.lower()):
        return None
    suffix = _slack_tool_suffix(tool_name).lower()
    fields = _SLACK_MCP_WRITE_TOOLS.get(suffix)
    if fields is None:
        return None
    for field_name in fields:
        value = tool_input.get(field_name)
        if isinstance(value, str) and value:
            return value
    # The tool IS a write surface but no body field was populated — scan
    # an empty payload (clean by construction) rather than fail-closed.
    return ""


def extract_publish_payload(tool_name: str, tool_input: ToolInput, cwd: Path | None = None) -> str | None:
    """Return the text-to-scan from a tool invocation, or ``None`` if not a publish.

    The ``None`` return is the gate's pass-through signal — the
    PreToolUse handler skips its work for any tool call that does not
    intend to publish.

    Bash commands are tokenized via the shell lexer so the publish-
    detection substring matcher and the body-extractor see the same
    logical token stream bash itself would execute (codex round-2 #2/#3,
    round-3 #1/#2/#3/#4).

    ``cwd`` is the harness-provided working directory, threaded into the body
    extractor so a ``gh pr edit --body-file <relpath>`` / ``--body "$(cat
    <relpath>)"`` body is RESOLVED against the dir the command actually runs in
    and SCANNED — instead of fail-closing on an unreadable relative path from the
    cold hook's reset cwd (#1213). Mirrors the banned-terms gate, which already
    threads it.
    """
    if tool_name == "Bash":
        raw_command = tool_input.get("command", "")
        if not _is_publish_command(raw_command):
            return None
        return _extract_bash_payload(raw_command, cwd=cwd)

    return _extract_slack_mcp_payload(tool_name, tool_input)


# ── Agent/Task dispatch-prompt body extraction (#1401) ──────────────

# The harness names the sub-agent dispatch vehicle ``Agent`` or ``Task``;
# both carry a ``prompt`` (the dispatched brief) and a short
# ``description`` (the one-line subject). Both are scanned so a verbatim
# user quote pasted into EITHER field is caught at the dispatch boundary —
# before the sub-agent loads it into context and can echo it into a later
# published output (the #1213 publish gate fires too late for that).
_DISPATCH_TOOLS: Final[frozenset[str]] = frozenset({"Agent", "Task"})
_DISPATCH_PROMPT_FIELDS: Final[tuple[str, ...]] = ("description", "prompt")


def extract_dispatch_payload(tool_name: str, tool_input: ToolInput) -> str | None:
    """Return the dispatch-prompt text to scan, or ``None`` for non-dispatch tools.

    The ``None`` return is the gate's pass-through signal — the PreToolUse
    handler skips its work for any tool that is not an ``Agent``/``Task``
    dispatch. The ``description`` (subject) and ``prompt`` (brief) fields
    are joined so a single :func:`scan_text` pass covers a quote pasted
    into either. A dispatch with no populated body scans the empty string
    (clean by construction) rather than failing closed.
    """
    if tool_name not in _DISPATCH_TOOLS:
        return None
    parts: list[str] = []
    for field_name in _DISPATCH_PROMPT_FIELDS:
        value = tool_input.get(field_name)
        if isinstance(value, str) and value:
            parts.append(value)
    return "\n".join(parts)


# ── Override + ledger ──────────────────────────────────────────────


def _segment_leads_with_env_override(words: list[str]) -> bool:
    """Return True iff ``words`` leads with ``QUOTE_OK=1`` before its command.

    Only the leading run of ``KEY=value`` env-assignment tokens is inspected:
    bash applies a leading inline assignment to that command's environment, while
    a ``KEY=val``-shaped token after the command name is an argument, not an
    override. The first non-assignment token ends the run.
    """
    for word in words:
        name, sep, value = word.partition("=")
        if not sep:
            return False
        if name == _QUOTE_OK_ENV:
            return value.strip() == "1"
    return False


def _segment_carries_override(words: list[str]) -> bool:
    return "--quote-ok" in words or _segment_leads_with_env_override(words)


def _publish_segment_carries_override(command: str) -> bool:
    """Return True iff the segment carrying the publish also carries the override.

    The override (``--quote-ok`` flag or a leading ``QUOTE_OK=1`` inline
    env-assignment) is honoured only when it rides the segment that IS ITSELF
    the publish (checked via :func:`_is_publish_command` on the standalone
    segment). This honours the common sub-agent shape that navigates first
    (``cd <worktree> && QUOTE_OK=1 gh pr create …`` or ``cd <wt> && gh pr
    create … --quote-ok``) while a decoy override on an unrelated segment cannot
    vouch for a chained publish elsewhere (``echo --quote-ok && gh pr create …``
    and ``gh pr create … ; echo --quote-ok`` both still fire). Mirrors
    ``banned_terms_scanner._has_leading_env_override`` (#2031, #2034).
    """
    for words in _segment_word_lists_raw(command):
        if _segment_carries_override(words) and _is_publish_command(" ".join(words)):
            return True
    return False


def has_quote_ok_override(tool_name: str, tool_input: ToolInput) -> bool:
    """Return True iff the caller explicitly opted out of the gate.

    The ``--quote-ok`` flag and a leading ``QUOTE_OK=1`` inline env-assignment
    are both honoured only when they ride the command segment that IS ITSELF the
    publish the gate would scan (:func:`_publish_segment_carries_override`). The
    common sub-agent shape navigates first (``cd <worktree> && QUOTE_OK=1 gh pr
    create …`` / ``cd <wt> && gh pr create … --quote-ok``), so the override lives
    on a NON-leading segment; checking each segment's own override against
    whether that segment is the publish honours it while a decoy on an unrelated
    segment cannot vouch for a chained publish elsewhere. Mirrors the #2031
    per-segment scoping in ``banned_terms_scanner.has_override``.

    ``QUOTE_OK=1`` is also honoured from the process environment
    (``os.environ``) — the documented env escape (#1213 AC §3). The Claude Code
    PreToolUse payload for a ``Bash`` tool carries NO ``env`` block, so the
    agent's ``QUOTE_OK=1`` lives in the hook subprocess's own environment;
    reading only ``tool_input["env"]`` meant the documented escape never reached
    the wrapper and the gate could only be cleared by paraphrasing (#126).
    ``tool_input["env"]`` is still consulted for any harness build that DOES
    populate it.
    """
    if tool_name == "Bash" and _publish_segment_carries_override(tool_input.get("command", "")):
        return True
    if os.environ.get(_QUOTE_OK_ENV, "").strip() == "1":
        note_env_override_once(_QUOTE_OK_ENV)
        return True
    env = tool_input.get("env") or {}
    return env.get(_QUOTE_OK_ENV, "").strip() == "1"


# In-prompt opt-out token for the dispatch-prompt gate (#1401). Unlike the
# publish-side ``--quote-ok`` flag / ``QUOTE_OK=1`` env (shell/env concepts
# that have no analogue inside an Agent/Task prompt body), the dispatch gate
# opt-out is an in-prompt token mirroring the existing
# ``[skip-skill-gate: <reason>]`` convention in ``hook_router``. The reason is MANDATORY — an empty reason does not
# bypass — so an audit can read WHY a quote-shaped dispatch was sanctioned.
_DISPATCH_QUOTE_OK_RE: Final[re.Pattern[str]] = re.compile(r"\[quote-ok:\s*(\S[^\]]*?)\s*\]")


def dispatch_quote_ok_reason(text: str) -> str | None:
    """Return the reason from a ``[quote-ok: <reason>]`` token in ``text``, else None.

    Scans only the first 512 characters (mirroring the
    ``hook_router._agent_prompt_skip_token`` precedent) so a token buried
    deep in a long dispatch body cannot silently authorise the whole
    prompt. An empty reason is rejected (returns ``None``).
    """
    match = _DISPATCH_QUOTE_OK_RE.search(text[:512])
    if not match:
        return None
    return match.group(1).strip() or None


def _ledger_path() -> Path:
    return hook_state_root() / "quote-scanner.jsonl"


def log_decision(
    *,
    tool_name: str,
    decision: str,
    result: ScanResult,
    override: bool,
    ledger_path: Path | None = None,
) -> None:
    """Append a one-line JSON record to the gate's audit ledger."""
    record = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "tool": tool_name,
        "decision": decision,
        "override": override,
        "high": [f.name for f in result.high],
        "medium": [f.name for f in result.medium],
    }
    target = ledger_path if ledger_path is not None else _ledger_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        # The ledger is best-effort — never block on a write failure.
        return


def format_block_message(result: ScanResult) -> str:
    """Render the PreToolUse deny reason for a HIGH match.

    The false-positive escape names the leading ``QUOTE_OK=1`` env PREFIX, not a
    ``--quote-ok`` CLI flag: the flag is consumed by the gate's parser, never by
    the posting command, so a ``t3 review post-comment`` (or any other
    subcommand) would reject it as an unknown option. The env prefix is a real
    shell construct every command accepts and is the spelling that actually
    works at the prompt.
    """
    names = ", ".join(sorted({f.name for f in result.high}))
    return (
        "BLOCKED: pre-publish quote-scanner gate (#1213). "
        f"Matched patterns: {names}. "
        "Paraphrase any user-attributed content; do not quote verbatim. "
        "If the match is a false positive, re-issue the command with a leading "
        "QUOTE_OK=1 env prefix (e.g. `QUOTE_OK=1 <command>`)."
    )


def format_dispatch_block_message(result: ScanResult) -> str:
    """Render the PreToolUse deny reason for a HIGH match in a dispatch prompt (#1401)."""
    names = ", ".join(sorted({f.name for f in result.high}))
    excerpt = next((f.excerpt for f in result.high if f.excerpt), "")
    matched = f' (e.g. "{excerpt}")' if excerpt else ""
    return (
        "BLOCKED: pre-dispatch quote-scanner gate (#1401). The Agent/Task prompt "
        f"carries verbatim user-voice/PII content{matched} — matched patterns: {names}. "
        "Paraphrase it into author-voice description before dispatching (the sub-agent "
        "would otherwise echo it into a published output, defeating the #1213 publish gate). "
        "If the match is a false positive, add `[quote-ok: <reason>]` near the start of the prompt."
    )


def format_warn_message(result: ScanResult) -> str:
    """Render the stderr warning for a MEDIUM-only match."""
    names = ", ".join(sorted({f.name for f in result.medium}))
    return (
        f"WARNING: pre-publish quote-scanner gate (#1213) — attribution patterns matched ({names}). "
        "Verify the content is paraphrased, not lifted from user speech."
    )
