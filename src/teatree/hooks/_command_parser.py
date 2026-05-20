"""Bash command surface parsing for the quote-scanner gate (#1213).

Extracted from :mod:`teatree.hooks.quote_scanner` to keep that module
under the project's per-file LOC ceiling. The public quote-scanner API
(scan_text, format_*, log_decision, extract_publish_payload,
has_quote_ok_override) lives in ``quote_scanner.py`` and delegates the
shell-grammar work to the helpers here.

The parser walks a Bash command string and pulls out every fragment
that could carry a publishable body — quoted ``--body``/``-m``/``-b``
flags, heredocs, file paths, ``gh api`` field assignments, ``curl``
data flags, and the JSON ``text``/``message``/``body`` fields nested
inside them. It also normalises shell-equivalent spellings
(line-continuations, ANSI-C ``$'...'`` quoting) so the substring
matcher and regexes see the same logical command Bash itself would
execute. Indirect body sources we cannot inspect (``gh api --input -``,
missing files, opaque ``-d @file`` references) fail closed via a
sentinel string that downstream scanning treats as a HIGH match.
"""

import codecs
import json
import re
from pathlib import Path
from typing import Final

# ── Publish-surface substring catalogues ────────────────────────────

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
    # by :func:`extract_bash_payload`.
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
    if not command.lstrip().startswith("t3 "):
        return False
    return any(needle in command for needle in _T3_PUBLISH_SUBSTRINGS)


def is_publish_command(command: str) -> bool:
    """Return True iff the Bash command would publish to an external surface."""
    if any(needle in command for needle in _BASH_PUBLISH_SUBSTRINGS):
        return True
    return _is_t3_publish_invocation(command)


# ── Body-flag and curl regexes ──────────────────────────────────────

# Capture --body / --description / --message / --title / -m / -F arg
# values plus heredocs. An optional ``$`` prefix on the opening quote
# covers ANSI-C quoting (``$'...'``) — codex round-2 #3.
_FLAG_VALUE_RE: Final[re.Pattern[str]] = re.compile(
    r"--(?:body|description|message|title)(?:[ =])\s*\$?(['\"])(.*?)\1",
    re.DOTALL,
)
_SHORT_M_RE: Final[re.Pattern[str]] = re.compile(r"\s-m\s+\$?(['\"])(.*?)\1", re.DOTALL)
_SHORT_B_RE: Final[re.Pattern[str]] = re.compile(r"\s-b\s+\$?(['\"])(.*?)\1", re.DOTALL)
_HEREDOC_RE: Final[re.Pattern[str]] = re.compile(
    r"<<\s*['\"]?(\w+)['\"]?\s*\n(.*?)\n\1\b",
    re.DOTALL,
)
_FILE_FLAG_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:--body-file|--description-file|--file)[ =]+(\S+)",
)
_GIT_SHORT_F_RE: Final[re.Pattern[str]] = re.compile(r"\s-F[ =]?(\S+)")
_API_FIELD_BODY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:-f|-F|--field|--raw-field)\s+body=\$?(['\"])?(.*?)(?(1)\1|(?=\s|$))",
    re.DOTALL,
)
_API_INPUT_FILE_RE: Final[re.Pattern[str]] = re.compile(r"--input[ =]+(\S+)")

# curl carries its payload in ``-d`` / ``--data`` / ``--data-raw`` /
# ``--data-binary`` / ``--json``. The ``[ =]`` separator matches both
# space and equals forms (codex round-2 #5). An optional ``$`` prefix
# covers ANSI-C-quoted bodies.
_CURL_DATA_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:--data-raw|--data-binary|--data|--json|\s-d)[ =]\s*\$?(['\"])(.*?)\1",
    re.DOTALL,
)
_CURL_DATA_FILE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:--data-raw|--data-binary|--data|--json|\s-d)[ =]@(\S+)",
)

# ``gh api ... --input -`` reads from stdin, which the hook cannot
# inspect. Detect via a dedicated regex so the fail-closed sentinel
# only fires for the genuine stdin form.
_API_INPUT_STDIN_RE: Final[re.Pattern[str]] = re.compile(r"--input[ =]-(?:\s|$)")

# Sentinel string that downstream scanning treats as a HIGH match. Any
# indirect or undecodable body source surfaces this so the gate fails
# closed (codex CRITICAL #5 round 1, codex round-2 #4).
FAIL_CLOSED_SENTINEL: Final[str] = (
    "the user said: pre-publish quote-scanner could not parse a body source — fail closed"
)


# ── Shell-grammar normalisation ─────────────────────────────────────


def normalize_line_continuations(command: str) -> str:
    r"""Collapse ``\<NL>`` line continuations to a single space.

    Bash treats ``\<NL>`` as a logical-line join — the next physical
    line continues the current command. We replace the sequence with a
    single space AND collapse surrounding whitespace so the joined
    token boundary matches what Bash itself would pass to the child
    process. The publish-substring matcher uses single-space separators
    like ``gh issue comment``, so source indentation has to disappear.
    """
    # ``[ws]*\<NL>[ws]*`` → one space.
    return re.sub(r"[ \t]*\\\r?\n[ \t]*", " ", command)


# ANSI-C ``$'...'`` — bash decodes escapes like ``\n`` / ``\x4c`` before
# passing the value to the child. Without decoding, an attacker can hide
# a HIGH-match body inside ``$'## User mandate\\nship'`` (codex
# round-2 #3).
_ANSI_C_QUOTED_RE: Final[re.Pattern[str]] = re.compile(r"\$'((?:[^'\\]|\\.)*)'")


def _decode_ansi_c_escapes(literal: str) -> str:
    r"""Decode a stripped ANSI-C body to its real bytes-as-string form."""
    try:
        return codecs.decode(literal, "unicode_escape")
    except (UnicodeDecodeError, ValueError):
        return literal


def expand_ansi_c_quotes(command: str) -> str:
    r"""Replace every ``$'...'`` token with a single-quoted decoded form.

    The result is a command string whose body flags use plain single
    quotes — letting the existing flag-value regexes match the now-
    decoded content. Single quotes inside the decoded text are escaped
    by switching to the ``'\''`` shell convention.
    """

    def _replace(match: re.Match[str]) -> str:
        decoded = _decode_ansi_c_escapes(match.group(1))
        escaped = decoded.replace("'", "'\\''")
        return f"'{escaped}'"

    return _ANSI_C_QUOTED_RE.sub(_replace, command)


def first_shell_command(command: str) -> str:
    """Return the prefix of ``command`` up to the first UNQUOTED newline.

    Quote-aware so a literal newline INSIDE a double- or single-quoted
    string is preserved (bash treats it as part of the same logical
    command), while a bare newline acts as a command separator. This is
    the substrate for the override-detection's first-segment rule:
    ``--quote-ok`` on a continuation line that bash would treat as a
    second command must not bypass the gate (codex round-2 #1).
    """
    state: str | None = None
    i = 0
    length = len(command)
    while i < length:
        ch = command[i]
        if state is None:
            if ch == "\\" and i + 1 < length:
                # Outside quotes, ``\X`` is a one-char escape (``\<NL>``
                # is line-continuation handled upstream by
                # :func:`normalize_line_continuations`).
                i += 2
                continue
            if ch in {"'", '"'}:
                state = ch
            elif ch in {"\n", "\r"}:
                return command[:i]
        elif ch == "\\" and state == '"' and i + 1 < length:
            # Backslash escapes only inside double-quotes.
            i += 2
            continue
        elif ch == state:
            state = None
        i += 1
    return command


# ── Body extraction ─────────────────────────────────────────────────


def _read_file_arg(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


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

    JSON-decoded bodies surface their ``text``/``message``/``body``
    fields. When curl carries a data flag we cannot inspect — a file
    reference (``@path``), stdin (``@-``), or unparsable body — the
    parser appends the fail-closed sentinel that trips the HIGH gate.
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
            payloads.append(FAIL_CLOSED_SENTINEL)
    # File-/stdin-referenced data flags cannot be inspected in-process.
    if _CURL_DATA_FILE_RE.search(command):
        payloads.append(FAIL_CLOSED_SENTINEL)
    return payloads


def extract_bash_payload(command: str) -> str:
    r"""Concatenate every body-like fragment the command surface carries.

    Pre-processing normalises shell-equivalent spellings before the
    pattern catalogue runs: ``\<NL>`` line continuations are joined and
    ANSI-C ``$'...'`` quoting is decoded (codex round-2 #2 / #3).
    Indirect body sources (``gh api --input -``, missing input files)
    fail closed via the sentinel.
    """
    command = normalize_line_continuations(command)
    command = expand_ansi_c_quotes(command)
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
        else:
            parts.append(FAIL_CLOSED_SENTINEL)
    # ``gh api / glab api --input -`` reads from stdin — fail closed
    # before per-file processing runs against the ``-`` literal.
    if _API_INPUT_STDIN_RE.search(command):
        parts.append(FAIL_CLOSED_SENTINEL)
    for match in _API_INPUT_FILE_RE.finditer(command):
        path_arg = match.group(1)
        if path_arg == "-":
            # Already handled by the stdin sentinel above.
            continue
        content = _read_file_arg(path_arg)
        if content is None:
            parts.append(FAIL_CLOSED_SENTINEL)
            continue
        parts.append(content)
        parts.extend(_json_body_fields(content))
    return "\n".join(parts)
