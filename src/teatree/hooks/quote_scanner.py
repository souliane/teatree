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
import shlex
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, TypedDict

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


def scan_text(text: str, *, blocklist_path: Path | None = None) -> ScanResult:
    """Match every built-in pattern and every blocklist regex against ``text``."""
    result = ScanResult()
    if not text:
        return result
    for pattern in (*_BUILTIN_PATTERNS, *_load_blocklist_patterns(blocklist_path)):
        match = pattern.regex.search(text)
        if match is None:
            continue
        excerpt = match.group(0)[:120]
        result.findings.append(Finding(name=pattern.name, severity=pattern.severity, excerpt=excerpt))
    return result


# ── Bash / t3 command surface parsing ──────────────────────────────

# Bash commands that publish to an external surface. The substring match
# is sufficient — Bash strings come from the LLM, not from a shell, so
# we don't have to worry about ``echo "gh issue create" | grep``-style
# embedding.
_BASH_PUBLISH_SUBSTRINGS: Final[tuple[str, ...]] = (
    "gh issue create",
    "gh issue edit",
    "gh issue comment",
    "gh pr create",
    "gh pr edit",
    "gh pr comment",
    "gh pr review",
    "glab issue create",
    "glab issue update",
    "glab issue note create",
    # ``glab issue note <id>`` (no ``create`` segment) is the real
    # comment subcommand — trailing space pins the substring to the
    # subcommand boundary so ``glab issue notebook`` would not match.
    "glab issue note ",
    "glab mr create",
    "glab mr update",
    "glab mr note create",
    "glab mr note ",
    "git commit -m",
    "git commit --message",
    "git commit -F",
    "git commit --file",
    "git tag --message",
    # ``gh api`` / ``glab api`` POST/PATCH calls land on REST endpoints
    # that publish issue/PR/MR comments. The payload is carried via
    # ``-f``/``-F``/``--raw-field``/``--field``/``--input`` and parsed
    # by :func:`_extract_bash_payload`.
    "gh api ",
    "glab api ",
    "chat.postMessage",
)

# t3 sub-commands that publish on the user's behalf. The overlay segment
# between ``t3`` and the verb is arbitrary (one of the registered
# overlays), so we match the verb-segment substring directly — e.g.
# ``review post-comment`` matches both ``t3 teatree review post-comment``
# and the equivalent per-overlay variant.
_T3_PUBLISH_SUBSTRINGS: Final[tuple[str, ...]] = (
    "notify send",
    "review post-comment",
    "review post-draft-note",
    "ticket create-issue",
    "t3 slack react",
)


def _is_t3_publish_invocation(command: str) -> bool:
    # All overlay-routed verbs go through ``t3`` — restrict the substring
    # match to commands that actually start with the ``t3`` binary so a
    # ``git log --grep "review post-comment"`` does not trip the gate.
    if not command.lstrip().startswith("t3 "):
        return False
    return any(needle in command for needle in _T3_PUBLISH_SUBSTRINGS)


def _is_publish_command(command: str) -> bool:
    if any(needle in command for needle in _BASH_PUBLISH_SUBSTRINGS):
        return True
    return _is_t3_publish_invocation(command)


# Capture --body / --description / --message / --title / -m / -F arg
# values plus heredocs. Heredocs cover the standard ``$(cat <<'EOF' …
# EOF)`` shape the rest of the codebase already uses for multi-line PR
# bodies.
_FLAG_VALUE_RE: Final[re.Pattern[str]] = re.compile(
    r"--(?:body|description|message|title)(?:[ =])\s*(['\"])(.*?)\1",
    re.DOTALL,
)
_SHORT_M_RE: Final[re.Pattern[str]] = re.compile(r"\s-m\s+(['\"])(.*?)\1", re.DOTALL)
# ``gh pr comment`` / ``gh issue comment`` use ``-b`` for the body. We
# only ever invoke the short-flag parser from inside a publish surface
# (``_is_publish_command`` already gated us), so a global ``-b`` shape
# match is safe.
_SHORT_B_RE: Final[re.Pattern[str]] = re.compile(r"\s-b\s+(['\"])(.*?)\1", re.DOTALL)
_HEREDOC_RE: Final[re.Pattern[str]] = re.compile(
    r"<<\s*['\"]?(\w+)['\"]?\s*\n(.*?)\n\1\b",
    re.DOTALL,
)
_FILE_FLAG_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:--body-file|--description-file|--file)[ =]+(\S+)",
)
_GIT_SHORT_F_RE: Final[re.Pattern[str]] = re.compile(r"\s-F[ =]?(\S+)")

# ``gh api`` / ``glab api`` carry their JSON payload in ``-f key=value``
# (string), ``-F key=value`` / ``--field`` (typed), ``--raw-field`` (raw
# string), or ``--input <file>`` (full JSON blob). We pluck any
# ``body=…`` assignment regardless of quoting, then read ``--input``
# files separately and scan a ``body`` JSON field.
_API_FIELD_BODY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:-f|-F|--field|--raw-field)\s+body=(['\"])?(.*?)(?(1)\1|(?=\s|$))",
    re.DOTALL,
)
_API_INPUT_FILE_RE: Final[re.Pattern[str]] = re.compile(r"--input[ =]+(\S+)")

# curl carries its payload in ``-d`` / ``--data`` / ``--data-raw`` /
# ``--data-binary`` / ``--json``. The body is JSON-shaped for the Slack
# / GitLab / GitHub surfaces this gate cares about — we JSON-decode and
# scan the ``text``/``message``/``body`` fields.
_CURL_DATA_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:--data-raw|--data-binary|--data|--json|\s-d)\s+(['\"])(.*?)\1",
    re.DOTALL,
)
# When curl's data flag references a file (``-d @path``) or stdin
# (``-d @-``) we cannot inspect the body in-process. Fail closed by
# returning a sentinel payload that trips the HIGH gate.
_CURL_DATA_FILE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:--data-raw|--data-binary|--data|--json|\s-d)\s+@(\S+)",
)
_CURL_FAIL_CLOSED_SENTINEL: Final[str] = (
    "the user said: pre-publish quote-scanner could not parse curl data flag — fail closed"
)


def _read_file_arg(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def extract_publish_payload(tool_name: str, tool_input: ToolInput) -> str | None:
    """Return the text-to-scan from a tool invocation, or ``None`` if not a publish.

    The ``None`` return is the gate's pass-through signal — the
    PreToolUse handler skips its work for any tool call that does not
    intend to publish.
    """
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        if not _is_publish_command(command):
            return None
        return _extract_bash_payload(command)

    if tool_name.startswith("mcp__") and "slack" in tool_name.lower() and "send" in tool_name.lower():
        return tool_input.get("text") or tool_input.get("message") or ""

    return None


def _extract_bash_payload(command: str) -> str:
    """Concatenate every body-like fragment the command surface carries.

    A single Bash invocation can carry the body in several forms — an
    inline ``--body`` quoted arg, a ``-m`` short flag, a heredoc spliced
    in via ``$(cat <<EOF … EOF)``, or a ``--body-file`` path. The gate
    scans the union so a payload split across forms still trips.
    """
    parts: list[str] = [match.group(2) for match in _FLAG_VALUE_RE.finditer(command)]
    parts.extend(match.group(2) for match in _SHORT_M_RE.finditer(command))
    parts.extend(match.group(2) for match in _SHORT_B_RE.finditer(command))
    parts.extend(match.group(2) for match in _HEREDOC_RE.finditer(command))
    parts.extend(match.group(2) for match in _API_FIELD_BODY_RE.finditer(command))
    parts.extend(_extract_curl_payloads(command))
    for match in (*_FILE_FLAG_RE.finditer(command), *_GIT_SHORT_F_RE.finditer(command)):
        content = _read_file_arg(match.group(1))
        if content is not None:
            parts.append(content)
    for match in _API_INPUT_FILE_RE.finditer(command):
        content = _read_file_arg(match.group(1))
        if content is None:
            continue
        parts.append(content)
        parts.extend(_json_body_fields(content))
    return "\n".join(parts)


def _json_body_fields(blob: str) -> list[str]:
    """Return ``text``/``message``/``body`` values from a JSON blob, if any."""
    try:
        decoded = json.loads(blob)
    except (ValueError, TypeError):
        return []
    if not isinstance(decoded, dict):
        return []
    return [str(decoded[key]) for key in ("text", "message", "body") if key in decoded]


def _extract_curl_payloads(command: str) -> list[str]:
    """Parse curl ``-d``/``--data``/``--data-raw``/``--json`` JSON bodies.

    The body is JSON-decoded and the ``text``/``message``/``body`` fields
    are extracted. When curl carries a data flag we cannot inspect — a
    file reference (``@path``), stdin (``@-``), or an unparsable body —
    the parser appends a fail-closed sentinel that trips the HIGH gate.
    The sentinel is the explicit ``fail-closed`` behaviour from the
    Codex CRITICAL #5 finding.
    """
    payloads: list[str] = []
    for match in _CURL_DATA_RE.finditer(command):
        raw = match.group(2)
        # Always include the raw text — the scanner's pattern catalogue
        # can match user-attributed prose even outside JSON shapes.
        payloads.append(raw)
        json_fields = _json_body_fields(raw)
        if json_fields:
            payloads.extend(json_fields)
        elif raw.strip().startswith(("{", "[")):
            # Looked like JSON but did not decode — fail closed.
            payloads.append(_CURL_FAIL_CLOSED_SENTINEL)
    # File-/stdin-referenced data flags cannot be inspected in-process.
    if _CURL_DATA_FILE_RE.search(command):
        payloads.append(_CURL_FAIL_CLOSED_SENTINEL)
    return payloads


# ── Override + ledger ──────────────────────────────────────────────


def has_quote_ok_override(tool_name: str, tool_input: ToolInput) -> bool:
    """Return True iff the caller explicitly opted out of the gate.

    Two surfaces are accepted. The flag ``--quote-ok`` may appear as a
    standalone token in a Bash command (``shlex.split`` is used so a
    substring inside a quoted body cannot fake the flag). The env mapping
    on the tool input may set ``QUOTE_OK=1`` (the harness exposes the env
    block separately from the command string).
    """
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        try:
            # ``comments=True`` strips ``# …`` so a smuggled
            # ``# --quote-ok`` after the publish command does not become
            # a real token (Codex CRITICAL #1).
            tokens = shlex.split(command, comments=True, posix=True)
        except ValueError:
            tokens = command.split()
        # The override is valid only when it lives in the FIRST shell
        # segment — i.e. before any metacharacter that would route the
        # remainder to a separate command. ``shlex`` keeps ``;``, ``|``,
        # ``&``, ``&&`` and ``||`` as standalone tokens, so we just look
        # for ``--quote-ok`` strictly before the first such token.
        meta_tokens = {";", "|", "&", "&&", "||"}
        for entry in tokens:
            if entry in meta_tokens:
                break
            if entry == "--quote-ok":
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
