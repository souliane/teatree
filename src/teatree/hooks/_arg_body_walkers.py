r"""Per-command argument body extraction for the pre-publish gates.

Extracted from :mod:`teatree.hooks._command_parser` to keep that module
under the project's per-file LOC ceiling. This module owns the body /
payload EXTRACTION half of the parser: the flag catalogues, the per-command
argument walkers (``gh``/``glab``/``git`` body flags, file-body flags,
``api`` field assignments, ``curl`` data flags), and the in-command heredoc
resolution. Publish-surface DETECTION (which command shapes publish at all)
stays in :mod:`teatree.hooks._publish_detection`; command-segment routing and
the public ``extract_*`` entry points stay in :mod:`_command_parser`.

The walkers iterate over the WORD tokens of one command segment and append
each body-like fragment to a shared ``payloads`` list. Indirect body sources
the gate cannot inspect (``--input -``, opaque ``-d @file``, a missing
``git commit -F`` message file) fail closed via :data:`FAIL_CLOSED_SENTINEL`,
which downstream scanning treats as a HIGH match.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

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


# ── Heredoc-to-file resolution ──────────────────────────────────────

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
# redirect target path with the heredoc delimiter so :func:`heredoc_file_bodies`
# can resolve a ``-F <path>`` reference to the body the command is about
# to write there (#126).
_HEREDOC_TO_FILE_RE: Final[re.Pattern[str]] = re.compile(
    r">{1,2}\|?\s*(?P<path>'[^']+'|\"[^\"]+\"|\S+)\s+<<\s*['\"]?(?P<delim>\w+)['\"]?\s*\n(?P<body>.*?)\n(?P=delim)\b",
    re.DOTALL,
)


def heredoc_inline_bodies(command: str) -> list[str]:
    """Return every ``<<EOF … EOF`` heredoc body inlined in ``command``.

    Heredoc bodies live on physical lines after the command word, so they are
    matched against the raw command string rather than the lexed token stream.
    """
    return [match.group(2) for match in _HEREDOC_RE.finditer(command)]


def heredoc_file_bodies(command: str) -> dict[str, str]:
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


# ── Per-command argument-walker dispatch tables ─────────────────────

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


def walk_curl_args(words: list[str], payloads: list[str]) -> None:
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


def walk_body_flags(words: list[str], payloads: list[str]) -> None:
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


@dataclass(frozen=True)
class _ShortFileFlag:
    """Resolved value of a short ``-F`` body-file reference, with its token span.

    ``path`` is the file the ``-F`` points at; ``consumed`` is how many tokens
    the flag occupied (``2`` for the space-separated ``-F <path>`` form, ``1``
    for the attached ``-F<path>`` form). ``None`` is returned when the leader's
    ``-F`` at this position is not a body-file reference (so the caller advances
    by one and lets another walker handle it).
    """

    path: str
    consumed: int


def _short_f_body_file(leader: str, words: list[str], i: int) -> _ShortFileFlag | None:
    """Resolve the file a short ``-F`` at ``words[i]`` references, or ``None``.

    The short ``-F`` is overloaded across leaders:

    - ``git`` -- ALWAYS a file (the ``git commit -F`` message file), regardless
        of the value.
    - ``gh`` / ``glab`` -- the documented short form of ``--body-file`` on
        ``issue/pr create|comment`` etc., but ``-F name=value`` on ``api`` is a
        field assignment. The two are disambiguated by VALUE: a ``=``-free token
        is a body-file path; a ``name=value`` token is left to
        :func:`walk_api_fields` (returns ``None`` here).
    - any other leader -- never a body-file ``-F`` (returns ``None``).

    Both the space-separated (``-F <path>``) and attached (``-F<path>``) spellings
    are recognised.
    """
    word = words[i]
    is_git = leader == "git"
    is_gh_glab = leader in {"gh", "glab"}
    if not (is_git or is_gh_glab):
        return None
    if word == "-F" and i + 1 < len(words):
        nxt = words[i + 1]
        if is_git or "=" not in nxt:
            return _ShortFileFlag(path=nxt, consumed=2)
        return None
    attached = _attached_value(word, "-F")
    if attached is not None and (is_git or "=" not in attached):
        return _ShortFileFlag(path=attached, consumed=1)
    return None


def walk_body_file_flags(
    words: list[str],
    payloads: list[str],
    *,
    leader: str,
    heredoc_files: dict[str, str],
    fail_closed_body_file: bool,
) -> None:
    """Extract ``--body-file``/``--file``/``-F`` style file payloads.

    The long ``--body-file`` / ``--file`` forms apply to every leader; the short
    ``-F`` form is leader-scoped by :func:`_short_f_body_file` (``git``'s ``-F``
    is always a file; ``gh``/``glab``'s ``-F`` is a body-file only on a ``=``-free
    value, otherwise it is an ``api`` field assignment :func:`walk_api_fields`
    handles).

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
        short = _short_f_body_file(leader, words, i)
        if short is not None:
            _append_file_payload(
                short.path, payloads, heredoc_files, fail_closed=(leader == "git" or fail_closed_body_file)
            )
            i += short.consumed
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


def walk_api_fields(words: list[str], payloads: list[str]) -> None:
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


def api_field_values(words: list[str]) -> list[str]:
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
