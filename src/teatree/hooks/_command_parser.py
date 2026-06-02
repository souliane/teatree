r"""Bash command surface parsing for the quote-scanner gate (#1213).

Extracted from :mod:`teatree.hooks.quote_scanner` to keep that module
under the project's per-file LOC ceiling. The public quote-scanner API
(scan_text, format_*, log_decision, extract_publish_payload,
has_quote_ok_override) lives in ``quote_scanner.py`` and delegates the
shell-grammar work to the helpers here.

The parser walks a Bash command string in two passes:

1. :mod:`teatree.hooks._shell_lexer` produces a token stream where bash
    shell grammar is honoured (``\<NL>`` removed token-internally,
    ``;``/``|``/``&``/``&&``/``||`` emitted as standalone metachars
    regardless of whitespace, ANSI-C ``$'...'`` decoded properly per
    the bash man-page).
2. Per-command argument walkers iterate over the WORD tokens of each
    command segment and pull out body-flag values, heredoc-style content,
    and attached short-option payloads (``-d'{...}'``).

Indirect body sources we cannot inspect (``gh api --input -``, opaque
``-d @file`` references, a missing ``git commit -F`` message file) fail
closed via a sentinel string that downstream scanning treats as a HIGH
match. A missing ``gh``/``glab`` ``--body-file`` is the one exception:
an absent drafted PR/issue body is "needs-inline", not a leak, so it
contributes no payload rather than a fail-closed HIGH (#126).

Publish-surface DETECTION (which command shapes are a publish at all) lives
in :mod:`teatree.hooks._publish_detection`: the contiguous-substring catalogue
here plus the token-aware ``api`` / ``git commit`` / opaque-forge-transport
classifiers there, so an interspersed persistent flag cannot break detection
(#1672). This module owns body / title / secret-surface EXTRACTION.
"""

import json
import re
from pathlib import Path
from typing import Final

from teatree.hooks._publish_detection import (
    command_has_opaque_forge_transport,
    command_has_token_aware_publish_surface,
    extract_title_fragments,
    segment_word_lists,
)
from teatree.hooks._shell_lexer import Token, TokenKind, is_command_separator, split_commands, tokenize

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
    "chat.postMessage",
)
# ``gh api`` / ``glab api`` is NOT a contiguous-substring publish: a bare
# substring match flagged read-only GET calls (``gh api user``, ``gh api
# repos/o/r/commits/main``) as publishes, so the destination-aware gates
# over-blocked them (#1530). Raw-REST publishes are classified WRITE-only and
# flag-order-robust by the token-aware :func:`_publish_detection.segment_is_api_write`
# (effective method ≠ GET), reached via :func:`is_publish_command`.

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


# Sentinel string that downstream scanning treats as a HIGH match. Any
# indirect or undecodable body source surfaces this so the gate fails
# closed (codex CRITICAL #5 round 1, codex round-2 #4).
#
# The wording must NOT itself match any quote-scanner HIGH pattern,
# otherwise the gate self-matches its own injected sentinel and reports
# a bogus user-quote finding on a body it never actually saw (#126: the
# old "the user said: …" phrasing tripped ``the-user-said-colon``). The
# sentinel is recognised explicitly by :func:`is_fail_closed_sentinel`
# so a scanner can fail closed on a NAMED reason instead of an
# accidental content-pattern self-match.
FAIL_CLOSED_SENTINEL: Final[str] = "[teatree-gate] pre-publish scanner could not resolve a body source; failing closed"


def is_fail_closed_sentinel(text: str) -> bool:
    """Return True iff ``text`` carries the injected fail-closed sentinel.

    Callers fail closed on a NAMED reason rather than rely on the
    sentinel accidentally tripping a content pattern (#126). The sentinel
    can be one of several concatenated payload fragments, so a substring
    test is used rather than equality.
    """
    return FAIL_CLOSED_SENTINEL in text


def normalize_for_substring_match(command: str) -> str:
    r"""Return a publish-detection-friendly string for ``command``.

    Re-emits the lexed token stream as space-separated WORD tokens, with
    one space between command segments. This collapses ``\<NL>`` (both
    token-internal and between-token) and ANSI-C decoding so the
    publish-substring matcher sees the same logical command bash would
    execute.
    """
    tokens = tokenize(command)
    out: list[str] = []
    for tok in tokens:
        if is_command_separator(tok):
            out.append(" ")
        else:
            out.extend((tok.value, " "))
    return "".join(out)


def _is_t3_publish_invocation(joined: str) -> bool:
    if not joined.lstrip().startswith("t3 "):
        return False
    return any(needle in joined for needle in _T3_PUBLISH_SUBSTRINGS)


def is_publish_command(command: str) -> bool:
    """Return True iff the Bash command would publish to an external surface.

    The contiguous substring catalogue (:data:`_BASH_PUBLISH_SUBSTRINGS`)
    catches the common spellings; the token-aware per-segment checks
    (:func:`_publish_detection.command_has_token_aware_publish_surface`) catch
    the ``git [global-flags] commit`` after a ``-C``/``--git-dir`` flag and the
    raw-REST ``gh``/``glab api`` WRITE (effective method ≠ GET) regardless of
    flag ordering, so the body reaches the scanner. A read-only ``gh``/``glab
    api`` GET is NOT a publish and is not flagged (#1530).
    """
    joined = normalize_for_substring_match(command)
    if any(needle in joined for needle in _BASH_PUBLISH_SUBSTRINGS):
        return True
    if _is_t3_publish_invocation(joined):
        return True
    return command_has_token_aware_publish_surface(command)


# ── Body-flag and curl regexes (heredoc only — flag args are token-aware) ─

_HEREDOC_RE: Final[re.Pattern[str]] = re.compile(
    r"<<\s*['\"]?(\w+)['\"]?\s*\n(.*?)\n\1\b",
    re.DOTALL,
)

# A redirect (``> path`` / ``>| path`` / ``>> path``) that writes a
# heredoc body to a file, e.g. ``cat > /tmp/msg.txt <<'EOF' … EOF``. The
# common agent idiom is to write a commit message to a temp file and
# then ``git commit -F /tmp/msg.txt`` — at PreToolUse scan time that file
# does NOT exist yet (the hook runs BEFORE the command), so the only
# place the body lives is the in-command heredoc. This regex pairs the
# redirect target path with the heredoc delimiter so :func:`extract_bash_payload`
# can resolve a ``-F <path>`` reference to the body the command is about
# to write there (#126).
_HEREDOC_TO_FILE_RE: Final[re.Pattern[str]] = re.compile(
    r">{1,2}\|?\s*(?P<path>'[^']+'|\"[^\"]+\"|\S+)\s+<<\s*['\"]?(?P<delim>\w+)['\"]?\s*\n(?P<body>.*?)\n(?P=delim)\b",
    re.DOTALL,
)


def _heredoc_file_bodies(command: str) -> dict[str, str]:
    """Map each ``> path <<EOF … EOF`` redirect target to its heredoc body.

    Resolves the agent idiom of writing a body to a temp file and then
    referencing it via ``git commit -F <path>``. The path is normalised
    (surrounding quotes stripped) so a quoted redirect target matches the
    later bare ``-F`` reference (#126).
    """
    bodies: dict[str, str] = {}
    for match in _HEREDOC_TO_FILE_RE.finditer(command):
        raw_path = match.group("path")
        path = raw_path.strip("'\"")
        bodies[path] = match.group("body")
    return bodies


# Per-command argument-walker dispatch tables --------------------------

# Body-bearing long options (value follows the flag as next token or
# attached via ``=``). The catalogue is shared by all publishing
# commands — gh, glab, git, curl all use the same long-option grammar.
_BODY_FLAG_NAMES: Final[frozenset[str]] = frozenset(
    {"--body", "--description", "--message", "--title"},
)

# Long options that point at a FILE whose content we should read. If the
# file is missing or unreadable the parser appends the fail-closed
# sentinel.
_BODY_FILE_FLAG_NAMES: Final[frozenset[str]] = frozenset(
    {"--body-file", "--description-file", "--file"},
)

# Short body-bearing flags used by ``gh`` / ``glab`` / ``git commit``.
_BODY_SHORT_FLAGS: Final[frozenset[str]] = frozenset({"-m", "-b"})

# Long options for ``gh api`` / ``glab api`` field assignments.
_API_FIELD_LONG_FLAGS: Final[frozenset[str]] = frozenset({"--field", "--raw-field"})
_API_FIELD_SHORT_FLAGS: Final[frozenset[str]] = frozenset({"-f", "-F"})

# Curl long-option data flags — payload is JSON-or-text.
_CURL_DATA_LONG_FLAGS: Final[frozenset[str]] = frozenset(
    {"--data", "--data-raw", "--data-binary", "--data-urlencode", "--json"},
)


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


def _attached_value(token: str, prefix: str) -> str | None:
    """Return the attached value of ``-X<value>`` / ``-X=<value>``, if any.

    Returns the substring AFTER ``prefix`` when ``token`` starts with the
    prefix and is strictly longer than it. ``-X=value`` strips the
    leading ``=`` so callers see the bare payload.
    """
    if token.startswith(prefix) and len(token) > len(prefix):
        return token[len(prefix) :].removeprefix("=")
    return None


def _scan_curl_payload(raw: str, payloads: list[str]) -> None:
    """Append ``raw`` plus its JSON ``text``/``message``/``body`` fields.

    A non-JSON-decodable body that LOOKS like JSON (starts with ``{`` or
    ``[``) fails closed because we cannot be sure the gate's pattern
    catalogue covers the obfuscation.
    """
    payloads.append(raw)
    json_fields = _json_body_fields(raw)
    if json_fields:
        payloads.extend(json_fields)
    elif raw.strip().startswith(("{", "[")):
        payloads.append(FAIL_CLOSED_SENTINEL)


def _record_curl_value(value: str, payloads: list[str]) -> None:
    """Route a single curl data value to the payload list.

    ``@file`` references fail closed (we cannot read arbitrary files);
    everything else gets the standard JSON-aware scan.
    """
    if value.startswith("@"):
        payloads.append(FAIL_CLOSED_SENTINEL)
    else:
        _scan_curl_payload(value, payloads)


def _curl_long_flag_attached(word: str) -> str | None:
    """Return the value of ``--data=VALUE`` / ``--json=VALUE`` if attached."""
    for flag in _CURL_DATA_LONG_FLAGS:
        attached = _attached_value(word, flag + "=")
        if attached is not None:
            return attached
    return None


def _curl_short_d_attached(word: str) -> str | None:
    """Return the value of ``-dVALUE`` attached short option, if applicable.

    Excludes the long ``--data*`` / ``--json`` family — those start with
    ``--`` and are handled by :func:`_curl_long_flag_attached`.
    """
    if word.startswith(("--data", "--json")):
        return None
    return _attached_value(word, "-d")


def _walk_curl_args(words: list[str], payloads: list[str]) -> None:
    """Extract curl ``-d``/``--data*``/``--json`` payloads from a command.

    Supports:
    - ``-d value`` (next token)
    - ``-dvalue`` (attached short option, POSIX)
    - ``-d=value`` (equals form)
    - ``--data value`` / ``--data=value``
    - ``-d@file`` / ``--data @file`` (fail closed — we cannot read the file)
    """
    i = 0
    n = len(words)
    while i < n:
        word = words[i]
        if word == "-d" and i + 1 < n:
            _record_curl_value(words[i + 1], payloads)
            i += 2
            continue
        if word in _CURL_DATA_LONG_FLAGS and i + 1 < n:
            _record_curl_value(words[i + 1], payloads)
            i += 2
            continue
        attached_short = _curl_short_d_attached(word)
        if attached_short is not None:
            _record_curl_value(attached_short, payloads)
            i += 1
            continue
        attached_long = _curl_long_flag_attached(word)
        if attached_long is not None:
            _record_curl_value(attached_long, payloads)
        i += 1


def _walk_body_flags(words: list[str], payloads: list[str]) -> None:
    """Extract ``--body``/``-m``/``-b`` style payloads from a command.

    Handles both space-separated (``--body "x"``) and equals-separated
    (``--body=x``) forms.
    """
    i = 0
    n = len(words)
    while i < n:
        word = words[i]
        if word in _BODY_FLAG_NAMES and i + 1 < n:
            payloads.append(words[i + 1])
            i += 2
            continue
        for flag in _BODY_FLAG_NAMES:
            attached = _attached_value(word, flag + "=")
            if attached is not None:
                payloads.append(attached)
                break
        if word in _BODY_SHORT_FLAGS and i + 1 < n:
            payloads.append(words[i + 1])
            i += 2
            continue
        i += 1


def _walk_body_file_flags(
    words: list[str],
    payloads: list[str],
    *,
    is_git: bool,
    heredoc_files: dict[str, str],
    fail_closed_body_file: bool,
) -> None:
    """Extract ``--body-file``/``--file``/``-F`` style file payloads.

    The git-style ``-F <path>`` form is a file reference ONLY for the
    ``git`` command (codex round-3 #6 — ``gh api -F body=x`` is a field
    assignment, NOT a file reference). The ``is_git`` flag scopes the
    short-form ``-F`` reader.

    ``heredoc_files`` maps a path written by a ``> path <<EOF … EOF``
    redirect earlier in the same command to its body, so a ``-F <path>``
    reference resolves to the in-command heredoc when the file does not
    exist on disk yet (#126).

    ``fail_closed_body_file`` decides what an UNREADABLE ``gh``/``glab`` body
    file does — ``True`` (the destination-aware gates) appends the fail-closed
    sentinel, ``False`` (the quote scanner) appends nothing (#126). The git
    ``-F`` commit-message path always fails closed regardless.
    """
    i = 0
    n = len(words)
    while i < n:
        word = words[i]
        if word in _BODY_FILE_FLAG_NAMES and i + 1 < n:
            _append_file_payload(words[i + 1], payloads, heredoc_files, fail_closed=fail_closed_body_file)
            i += 2
            continue
        attached: str | None = None
        for flag in _BODY_FILE_FLAG_NAMES:
            attached = _attached_value(word, flag + "=")
            if attached is not None:
                _append_file_payload(attached, payloads, heredoc_files, fail_closed=fail_closed_body_file)
                break
        if attached is not None:
            i += 1
            continue
        if is_git and word == "-F" and i + 1 < n:
            _append_file_payload(words[i + 1], payloads, heredoc_files, fail_closed=True)
            i += 2
            continue
        if is_git:
            attached = _attached_value(word, "-F")
            if attached is not None:
                _append_file_payload(attached, payloads, heredoc_files, fail_closed=True)
                i += 1
                continue
        i += 1


def _append_file_payload(path: str, payloads: list[str], heredoc_files: dict[str, str], *, fail_closed: bool) -> None:
    """Append the body referenced by a ``-F``/``--file``/``--body-file`` path.

    Resolution order: the on-disk file, then an in-command heredoc that
    writes to that path (``cat > path <<EOF … EOF``), then the
    ``fail_closed`` branch. The heredoc fallback closes the #126 false
    positive where a body written to a temp file and committed via ``-F``
    in the same command was unreadable at PreToolUse scan time (the hook
    runs BEFORE the file is created).

    ``fail_closed`` selects what an unresolvable path does. ``True`` appends
    the fail-closed sentinel: the ``git commit -F <path>`` commit-message path
    always uses it (#1207), as does a ``gh``/``glab`` body file for the
    destination-aware banned-terms / bare-reference scanners, so a PUBLIC post
    whose body the gate cannot read hard-blocks rather than slip through unread
    (a destination-internal post is skipped before the payload is scanned, so
    the sentinel never over-blocks it). ``False`` appends NOTHING — the quote
    scanner keeps a drafted-but-absent ``gh``/``glab`` body file as
    "needs-inline", not a fail-closed HIGH (#126).
    """
    content = _read_file_arg(path)
    if content is None:
        content = heredoc_files.get(path)
    if content is not None:
        payloads.append(content)
    elif fail_closed:
        payloads.append(FAIL_CLOSED_SENTINEL)


def _handle_api_input(arg: str, payloads: list[str]) -> None:
    """Read a ``--input`` argument: stdin or missing file → fail closed."""
    if arg == "-":
        payloads.append(FAIL_CLOSED_SENTINEL)
        return
    content = _read_file_arg(arg)
    if content is None:
        payloads.append(FAIL_CLOSED_SENTINEL)
        return
    payloads.append(content)
    payloads.extend(_json_body_fields(content))


def _walk_api_fields(words: list[str], payloads: list[str]) -> None:
    """Extract ``-f``/``-F``/``--field``/``--raw-field`` ``body=`` assignments.

    Also handles ``--input <file>`` / ``--input -`` (stdin → fail closed)
    and ``--input <missing>`` (fail closed). Field assignments other than
    ``body=`` are ignored.
    """
    field_flags = _API_FIELD_SHORT_FLAGS | _API_FIELD_LONG_FLAGS
    i = 0
    n = len(words)
    while i < n:
        word = words[i]
        if word in field_flags and i + 1 < n:
            _handle_field_assignment(words[i + 1], payloads)
            i += 2
            continue
        if word == "--input" and i + 1 < n:
            _handle_api_input(words[i + 1], payloads)
            i += 2
            continue
        attached = _attached_value(word, "--input=")
        if attached is not None:
            _handle_api_input(attached, payloads)
        i += 1


def _handle_field_assignment(arg: str, payloads: list[str]) -> None:
    """Parse a ``-F body=value`` style argument and append the value.

    The ``body=`` prefix is required — other field names (``title=``,
    etc.) are not body-bearing and are ignored.
    """
    if "=" not in arg:
        return
    name, _, value = arg.partition("=")
    if name == "body":
        payloads.append(value)


# ── Command-segment walking ─────────────────────────────────────────


def _first_two_words(segment: list[Token]) -> tuple[str, str]:
    """Return up to the first two WORD values of a command segment.

    Empty positions are returned as ``""``. Tokens that look like
    environment-variable assignments (``KEY=val``) appearing before the
    command name are skipped.
    """
    words = [tok.value for tok in segment if tok.kind is TokenKind.WORD]
    # Skip leading ENV=value assignments.
    while words and re.fullmatch(r"[A-Z_][A-Z0-9_]*=.*", words[0]):
        words = words[1:]
    first = words[0] if words else ""
    second = words[1] if len(words) > 1 else ""
    return first, second


def _walk_command_segment(
    segment: list[Token], payloads: list[str], heredoc_files: dict[str, str], *, fail_closed_body_file: bool
) -> None:
    """Route a single command segment to the right argument walkers."""
    words = [tok.value for tok in segment if tok.kind is TokenKind.WORD]
    if not words:
        return
    first, _ = _first_two_words(segment)
    # All segments get the generic body-flag walker since gh, glab, git,
    # and t3 all accept ``--body``/``--message``/``-m``/``-b``.
    _walk_body_flags(words, payloads)
    _walk_body_file_flags(
        words,
        payloads,
        is_git=(first == "git"),
        heredoc_files=heredoc_files,
        fail_closed_body_file=fail_closed_body_file,
    )
    # ``gh api`` / ``glab api`` field assignments.
    if first in {"gh", "glab"}:
        _walk_api_fields(words, payloads)
    if first == "curl":
        _walk_curl_args(words, payloads)


# ── Body extraction ─────────────────────────────────────────────────


def extract_bash_payload(command: str, *, fail_closed_body_file: bool = False) -> str:
    r"""Concatenate every body-like fragment the command surface carries.

    The command is tokenized once via :mod:`teatree.hooks._shell_lexer`
    so shell-equivalent spellings (line continuations both token-
    internal and between-token, ANSI-C ``$'...'`` quoting, unspaced
    metacharacters) collapse to the same logical token stream bash
    itself would execute. Then each command segment is routed to the
    right per-command argument walker.

    Indirect body sources (``gh api --input -``, missing files, opaque
    ``-d @file`` references) fail closed via the sentinel. A ``-F <path>``
    reference whose file is written by a ``> path <<EOF … EOF`` redirect
    in the same command resolves to that heredoc body instead (#126).

    ``fail_closed_body_file`` controls an UNREADABLE ``gh``/``glab`` body
    file: ``False`` (default, the quote scanner) keeps the #126 behaviour
    (an absent draft body contributes nothing); ``True`` (the
    destination-aware banned-terms / bare-reference gates) appends the
    fail-closed sentinel so a PUBLIC file-body post whose body the gate
    cannot read hard-blocks instead of slipping through unread.
    """
    parts: list[str] = []
    heredoc_files = _heredoc_file_bodies(command)
    tokens = tokenize(command)
    for segment in split_commands(tokens):
        _walk_command_segment(segment, parts, heredoc_files, fail_closed_body_file=fail_closed_body_file)
    # Heredocs still need to be parsed against the raw command — the
    # lexer treats them as regular content since heredoc bodies live on
    # subsequent physical lines. The regex below tolerates that shape.
    parts.extend(match.group(2) for match in _HEREDOC_RE.finditer(command))
    # A forge call hidden inside an interpreter / wrapper argument
    # (``sh -c "gh ... --body X"``, ``eval``, ``ssh host gh``, ``xargs gh``)
    # carries its body in an opaque token the walkers cannot descend into; the
    # destination-aware gates fail closed on it so an unscannable public post
    # hard-blocks rather than slips through unread.
    if fail_closed_body_file and command_has_opaque_forge_transport(command):
        parts.append(FAIL_CLOSED_SENTINEL)
    return "\n".join(parts)


# ── Secret-scan surfaces (#1672) ────────────────────────────────────


def _api_field_values(words: list[str]) -> list[str]:
    """Return EVERY ``-f``/``-F``/``--field``/``--raw-field`` field VALUE.

    The body extractor keeps only ``body=`` assignments; a secret can equally
    live in a ``-f title=`` or any other field of a ``gh api`` / ``glab api``
    call, so the secret scan reads every field value (the part after ``=``)
    regardless of field name. Bare values (no ``=``) are kept as-is.
    """
    field_flags = _API_FIELD_SHORT_FLAGS | _API_FIELD_LONG_FLAGS
    values: list[str] = []
    i = 0
    n = len(words)
    while i < n:
        word = words[i]
        if word in field_flags and i + 1 < n:
            values.append(words[i + 1].partition("=")[2] or words[i + 1])
            i += 2
            continue
        i += 1
    return values


def extract_secret_scan_text(command: str) -> str:
    """Concatenate EVERY surface a secret must be blocked on, regardless of destination.

    A secret leaks on ALL surfaces (a title, a short ``-t`` flag, a
    ``gh api -f title=`` field), not only the description body the carve-out
    is about. This widens the secret check beyond :func:`extract_bash_payload`
    to also cover the title / commit-subject fragments
    (:func:`extract_title_fragments`) and every ``gh``/``glab api`` field value
    (:func:`_api_field_values`), so :func:`publish_surface.contains_secret`
    sees them before the destination skip can short-circuit a scan.
    """
    parts = [extract_bash_payload(command, fail_closed_body_file=False)]
    parts.extend(extract_title_fragments(command))
    for words in segment_word_lists(command):
        if words[0] in {"gh", "glab"}:
            parts.extend(_api_field_values(words))
    return "\n".join(part for part in parts if part)


# ── Quote-OK override detection ─────────────────────────────────────


def first_segment_words(command: str) -> list[str]:
    """Return the WORD-value list of the FIRST command segment.

    Used by the override-detection: a ``--quote-ok`` token is only
    honoured when it appears as a CLI token in the first segment of the
    bash command. Anything after the first command-separator operator
    is a separate command and must not bypass the gate (codex round-2
    #1, round-3 #2).
    """
    tokens = tokenize(command)
    segments = split_commands(tokens)
    if not segments:
        return []
    return [tok.value for tok in segments[0] if tok.kind is TokenKind.WORD]
