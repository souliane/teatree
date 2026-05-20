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

The blocklist file at ``$T3_DATA_DIR/quote-blocklist.txt`` (default
``~/.teatree/quote-blocklist.txt``) is a *regex* list, not a quote
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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, TypedDict

from teatree.hooks._command_parser import extract_bash_payload as _extract_bash_payload
from teatree.hooks._command_parser import first_segment_words as _first_segment_words
from teatree.hooks._command_parser import is_publish_command as _is_publish_command

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


HIGH: Final[Severity] = "high"
MEDIUM: Final[Severity] = "medium"


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
    base = os.environ.get("T3_DATA_DIR")
    if base:
        return Path(base) / "quote-blocklist.txt"
    return Path.home() / ".teatree" / "quote-blocklist.txt"


def _load_blocklist_patterns(path: Path | None = None) -> list[Pattern]:
    """Compile regexes from the on-disk blocklist file.

    The file is a list of REGEX patterns (one per line) — never raw
    quotes. Blank lines and ``#``-prefixed comments are skipped. Each
    line is compiled with ``re.IGNORECASE``. Any line that fails to
    compile is reported via ``ValueError`` so a bad regex shows up
    immediately rather than silently disabling a rule.
    """
    target = path if path is not None else _blocklist_path()
    if not target.is_file():
        return []
    patterns: list[Pattern] = []
    for idx, raw in enumerate(target.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            compiled = re.compile(line, re.IGNORECASE)
        except re.error as exc:
            msg = f"invalid regex in {target}:{idx} — {exc}"
            raise ValueError(msg) from exc
        patterns.append(Pattern(name=f"blocklist:{idx}", severity=HIGH, regex=compiled))
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


def scan_text(text: str, *, blocklist_path: Path | None = None) -> ScanResult:
    """Match every built-in pattern and every blocklist regex against ``text``.

    Smart-quote variants in the input are normalised to ASCII before
    matching so a single regex catches all typographic forms.
    """
    result = ScanResult()
    if not text:
        return result
    normalized = _normalize_quotes(text)
    for pattern in (*_BUILTIN_PATTERNS, *_load_blocklist_patterns(blocklist_path)):
        match = pattern.regex.search(normalized)
        if match is None:
            continue
        excerpt = match.group(0)[:120]
        result.findings.append(Finding(name=pattern.name, severity=pattern.severity, excerpt=excerpt))
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


def extract_publish_payload(tool_name: str, tool_input: ToolInput) -> str | None:
    """Return the text-to-scan from a tool invocation, or ``None`` if not a publish.

    The ``None`` return is the gate's pass-through signal — the
    PreToolUse handler skips its work for any tool call that does not
    intend to publish.

    Bash commands are tokenized via the shell lexer so the publish-
    detection substring matcher and the body-extractor see the same
    logical token stream bash itself would execute (codex round-2 #2/#3,
    round-3 #1/#2/#3/#4).
    """
    if tool_name == "Bash":
        raw_command = tool_input.get("command", "")
        if not _is_publish_command(raw_command):
            return None
        return _extract_bash_payload(raw_command)

    return _extract_slack_mcp_payload(tool_name, tool_input)


# ── Override + ledger ──────────────────────────────────────────────


def has_quote_ok_override(tool_name: str, tool_input: ToolInput) -> bool:
    """Return True iff the caller explicitly opted out of the gate.

    Two surfaces are accepted. The flag ``--quote-ok`` may appear as a
    standalone token in the FIRST shell command segment — any
    ``--quote-ok`` that lives after a command-separator metacharacter
    (``;`` / ``|`` / ``&`` / ``&&`` / ``||`` / literal newline) is part
    of a SECOND command and must not bypass the gate (codex round-2 #1,
    round-3 #2). The shell lexer normalises unspaced metacharacters
    (``cmd "x";echo --quote-ok``) so the first-segment rule holds
    regardless of whitespace.

    The env mapping on the tool input may set ``QUOTE_OK=1`` (the
    harness exposes the env block separately from the command string).
    """
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        if "--quote-ok" in _first_segment_words(command):
            return True
    env = tool_input.get("env") or {}
    return env.get("QUOTE_OK", "").strip() == "1"


def _ledger_path() -> Path:
    base = os.environ.get("T3_DATA_DIR")
    root = Path(base) if base else Path.home() / ".teatree"
    return root / "quote-scanner.jsonl"


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
    """Render the PreToolUse deny reason for a HIGH match."""
    names = ", ".join(sorted({f.name for f in result.high}))
    return (
        "BLOCKED: pre-publish quote-scanner gate (#1213). "
        f"Matched patterns: {names}. "
        "Paraphrase any user-attributed content; do not quote verbatim. "
        "If the match is a false positive, re-issue the command with --quote-ok "
        "(or set QUOTE_OK=1 in the tool env)."
    )


def format_warn_message(result: ScanResult) -> str:
    """Render the stderr warning for a MEDIUM-only match."""
    names = ", ".join(sorted({f.name for f in result.medium}))
    return (
        f"WARNING: pre-publish quote-scanner gate (#1213) — attribution patterns matched ({names}). "
        "Verify the content is paraphrased, not lifted from user speech."
    )
