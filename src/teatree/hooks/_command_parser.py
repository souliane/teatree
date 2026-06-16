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
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final

from teatree.hooks._publish_detection import (
    command_has_opaque_forge_transport,
    command_has_token_aware_publish_surface,
    extract_title_fragments,
    segment_is_substring_publish,
    segment_word_lists,
)
from teatree.hooks._shell_lexer import Token, TokenKind, split_commands, tokenize

if TYPE_CHECKING:
    from teatree.hooks._body_file_resolution import BodyFileContext

# ── Publish-surface substring catalogues ────────────────────────────

# The ``gh``/``glab``/``git``/``curl`` contiguous-substring spellings now live
# in :data:`_publish_detection._LEADER_PUBLISH_SUBSTRINGS`, keyed by their owning
# leader so a read-only ``grep``/``cat``/``rg`` that merely QUOTES one is not a
# publish (the false positive). ``gh``/``glab api`` stays WRITE-only / token-aware
# (:func:`_publish_detection.segment_is_api_write`, effective method ≠ GET, #1530).

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


# A SECOND fail-closed sentinel for the body sources that are FUNDAMENTALLY
# unavailable at PreToolUse -- an unexpanded ``$VAR`` (the value is not in the
# hook env) and a stdin body (``gh api --input -``, ``git commit -F -``). The
# generic :data:`FAIL_CLOSED_SENTINEL` covers a missing/unreadable FILE, where
# the actionable advice is "the file is missing"; these two cases need the
# OPPOSITE advice (write the body to an absolute file and pass ``--body-file
# <abspath>``), so they carry a distinct sentinel the gate maps to a distinct,
# actionable message (#2369). The block is identical -- both sentinels fail
# closed -- only the operator-facing reason differs. It too must not self-match
# any quote-scanner HIGH pattern.
UNAVAILABLE_BODY_SOURCE_SENTINEL: Final[str] = (
    "[teatree-gate] pre-publish body source is unavailable before the command runs; failing closed"
)


def is_fail_closed_sentinel(text: str) -> bool:
    """Return True iff ``text`` carries an INJECTED fail-closed sentinel.

    The parser emits a sentinel as its own discrete payload fragment for every
    unresolvable/ambiguous body source, and the fragments are joined by newlines
    — so a genuinely-injected sentinel is always a standalone newline-delimited
    line equal to :data:`FAIL_CLOSED_SENTINEL` or
    :data:`UNAVAILABLE_BODY_SOURCE_SENTINEL`. The gate fails closed on either
    NAMED reason (#126/#2369); both spellings are recognised here so every
    downstream gate (quote-scanner, AI-signature, banned-terms) keeps failing
    closed regardless of which body-source class injected the sentinel.

    A body that merely MENTIONS a sentinel as inert prose inside a
    properly-quoted argument value (a commit message or PR body that
    DISCUSSES the gate) embeds the phrase mid-line, not as a standalone
    line. That is not a quoting hazard — the argument is correctly quoted —
    so the line-exact test allows it while every genuine injection (the
    sentinel on its own line) still fails closed (#1213).
    """
    sentinels = {FAIL_CLOSED_SENTINEL, UNAVAILABLE_BODY_SOURCE_SENTINEL}
    return any(line.strip() in sentinels for line in text.split("\n"))


def is_unavailable_body_source_sentinel(text: str) -> bool:
    """Return True iff ``text`` carries an injected UNAVAILABLE-body-source sentinel.

    Distinguishes the ``$VAR`` / stdin class (fundamentally unavailable before
    the command runs) from a missing/unreadable FILE, so the gate can render the
    actionable "write the body to an absolute file and use ``--body-file
    <abspath>``" message for it instead of the misleading "body file is missing"
    one (#2369). Line-exact for the same inert-prose reason as
    :func:`is_fail_closed_sentinel`.
    """
    return any(line.strip() == UNAVAILABLE_BODY_SOURCE_SENTINEL for line in text.split("\n"))


def _segment_is_t3_publish(words: list[str]) -> bool:
    """Return True iff ``words`` is a ``t3``-led segment carrying a publish verb.

    Keyed to the segment's own leading executable so a read-only
    ``grep "notify send"`` (leader ``grep``) is not misread as a ``t3`` post,
    and a ``cd <wt> && t3 <overlay> notify send`` (the publish verb on its own
    ``t3``-led segment) is correctly detected. The leader is canonicalised up to
    the ``t3`` executable basename so a path-form leader (``./t3``,
    ``/usr/local/bin/t3``) is recognised the same as a bare ``t3`` (env-prefixed
    leaders are already stripped by :func:`_publish_detection.segment_word_lists`).
    The overlay segment between ``t3`` and the verb is arbitrary, so the
    verb-segment substring is matched against the canonicalised joined words.
    """
    if PurePosixPath(words[0]).name != "t3":
        return False
    joined = " ".join(["t3", *words[1:]])
    return any(needle in joined for needle in _T3_PUBLISH_SUBSTRINGS)


def is_publish_command(command: str) -> bool:
    """Return True iff the Bash command would publish to an external surface.

    Detection is per-SEGMENT and keyed to each segment's own leading executable:

    - the leader-keyed substring catalogue
        (:func:`_publish_detection.segment_is_substring_publish`) catches the
        common ``gh``/``glab``/``git``/``curl`` spellings ONLY in a segment whose
        own leader is that tool -- so a read-only ``grep "glab mr create"`` /
        ``cat | grep "gh issue create"`` / ``rg "git commit -m"`` that merely
        QUOTES the spelling in an argument is NOT a publish;
    - the ``t3`` publish verbs (:func:`_segment_is_t3_publish`), likewise keyed
        to a ``t3``-led segment; and
    - the token-aware per-segment checks
        (:func:`_publish_detection.command_has_token_aware_publish_surface`) catch
        the ``git [global-flags] commit`` after a ``-C``/``--git-dir`` flag and the
        raw-REST ``gh``/``glab api`` WRITE (effective method ≠ GET) regardless of
        flag ordering. A read-only ``gh``/``glab api`` GET is NOT a publish (#1530).
    """
    for words in segment_word_lists(command):
        if segment_is_substring_publish(words) or _segment_is_t3_publish(words):
            return True
    return command_has_token_aware_publish_surface(command)


# Per-command argument-walker dispatch tables --------------------------

# Body-bearing long options (value follows the flag as next token or
# attached via ``=``). The catalogue is shared by all publishing
# commands — gh, glab, git, curl all use the same long-option grammar.
_BODY_FLAG_NAMES: Final[frozenset[str]] = frozenset(
    {"--body", "--description", "--message", "--title"},
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


def read_file_arg(path: str, base: Path | None = None) -> str | None:
    """Return the text of ``path``, trying ``base / path`` as a fallback.

    The bare ``path`` is read first (an absolute path, or one relative to the
    process cwd). When that fails and ``base`` is set, the same path is retried
    relative to ``base`` -- the dir whose repo a ``git commit`` LANDS in. At
    PreToolUse the cold hook subprocess's cwd has often reset away from the
    worktree, so a ``git -C <worktree> commit -F <relpath>`` body file is
    unreadable from the cwd yet readable from the commit's own repo dir.
    Resolving against ``base`` lets the gate scan that body and apply the
    private-repo carve-out instead of fail-closing on an unread body.
    """
    candidates = [Path(path)]
    if base is not None and not Path(path).is_absolute():
        candidates.append(base / path)
    for candidate in candidates:
        try:
            return candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
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


def attached_value(token: str, prefix: str) -> str | None:
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
        attached = attached_value(word, flag + "=")
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
    return attached_value(word, "-d")


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


def _walk_body_flags(words: list[str], payloads: list[str], base: "Path | None") -> None:
    """Extract ``--body``/``--description``/``--message``/``--title``/``-m``/``-b`` payloads.

    Handles both space-separated (``--body "x"``) and equals-separated
    (``--body=x``) forms. Each extracted value is passed through
    :func:`_body_file_resolution.resolve_inline_body_value`, which resolves a
    ``$(cat <path>)`` command substitution to the file content and a ``$VAR`` to
    its environment value (``base`` is the cold-hook cwd fallback for a relative
    cat path); an unresolvable indirection yields the fail-closed sentinel so the
    scan blocks rather than reads an unexpanded shell token.
    """
    from teatree.hooks._body_file_resolution import resolve_inline_body_value  # noqa: PLC0415

    i = 0
    n = len(words)
    while i < n:
        word = words[i]
        if word in _BODY_FLAG_NAMES and i + 1 < n:
            payloads.append(resolve_inline_body_value(words[i + 1], base))
            i += 2
            continue
        attached_handled = False
        for flag in _BODY_FLAG_NAMES:
            attached = attached_value(word, flag + "=")
            if attached is not None:
                payloads.append(resolve_inline_body_value(attached, base))
                attached_handled = True
                break
        if attached_handled:
            i += 1
            continue
        if word in _BODY_SHORT_FLAGS and i + 1 < n:
            payloads.append(resolve_inline_body_value(words[i + 1], base))
            i += 2
            continue
        i += 1


def _handle_api_input(arg: str, payloads: list[str]) -> None:
    """Read a ``--input`` argument: stdin or missing file → fail closed.

    A literal ``-`` is a STDIN body the PreToolUse hook cannot read before the
    command runs, so it carries :data:`UNAVAILABLE_BODY_SOURCE_SENTINEL` and the
    gate renders the actionable "write the body to an absolute file" message
    (#2369). A NAMED file that does not exist is a missing FILE, so it keeps the
    generic :data:`FAIL_CLOSED_SENTINEL` whose "body file is missing" advice fits.
    """
    if arg == "-":
        payloads.append(UNAVAILABLE_BODY_SOURCE_SENTINEL)
        return
    content = read_file_arg(arg)
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
        attached = attached_value(word, "--input=")
        if attached is not None:
            _handle_api_input(attached, payloads)
        i += 1


def _handle_field_assignment(arg: str, payloads: list[str]) -> None:
    """Parse a ``-F body=value`` style argument and append the RESOLVED value.

    The ``body=`` prefix is required — other field names (``title=``, etc.)
    are not body-bearing and are ignored. The value is passed through
    :func:`resolve_inline_body_value` so a ``-f body=$(cat <path>)`` /
    ``-f body=$VAR`` posts the file content / env value the scanner must read,
    not the unexpanded shell token (a leak inside the file would otherwise slip
    unscanned); an unresolvable indirection fails closed (#1415).
    """
    from teatree.hooks._body_file_resolution import resolve_inline_body_value  # noqa: PLC0415

    if "=" not in arg:
        return
    name, _, value = arg.partition("=")
    if name == "body":
        payloads.append(resolve_inline_body_value(value, None))


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


def _walk_command_segment(segment: list[Token], payloads: list[str], ctx: "BodyFileContext") -> None:
    """Route a single command segment to the right argument walkers."""
    from teatree.hooks._body_file_resolution import walk_body_file_flags  # noqa: PLC0415
    from teatree.hooks._t3_review_post import append_t3_review_note_payload  # noqa: PLC0415

    words = [tok.value for tok in segment if tok.kind is TokenKind.WORD]
    if not words:
        return
    first, _ = _first_two_words(segment)
    # All segments get the generic body-flag walker since gh, glab, git,
    # and t3 all accept ``--body``/``--message``/``-m``/``-b``.
    _walk_body_flags(words, payloads, ctx.base)
    # ``t3 review`` posts carry the body as the positional NOTE and use
    # ``--file`` as a diff ANCHOR, not a body-file — extract the NOTE and SKIP
    # the body-file walker so the anchored source is never scanned (#2278/#2270).
    if append_t3_review_note_payload(words, payloads):
        return
    walk_body_file_flags(words, payloads, leader=first, ctx=ctx)
    # ``gh api`` / ``glab api`` field assignments.
    if first in {"gh", "glab"}:
        _walk_api_fields(words, payloads)
    if first == "curl":
        _walk_curl_args(words, payloads)


# ── Body extraction ─────────────────────────────────────────────────


def extract_bash_payload(command: str, *, fail_closed_body_file: bool = False, cwd: Path | None = None) -> str:
    r"""Concatenate every body-like fragment the command surface carries.

    The command is tokenized once via :mod:`teatree.hooks._shell_lexer`
    so shell-equivalent spellings (line continuations both token-
    internal and between-token, ANSI-C ``$'...'`` quoting, unspaced
    metacharacters) collapse to the same logical token stream bash
    itself would execute. Then each command segment is routed to the
    right per-command argument walker.

    Indirect body sources (``gh api --input -``, missing files, opaque
    ``-d @file`` references) fail closed via the sentinel. A
    ``--body-file``/``-F <path>`` reference whose body is written earlier in
    the same command — by a ``> path <<EOF … EOF`` heredoc (#126) or by a
    ``printf``/``echo > path`` redirect, including when the path is the same
    unexpanded ``$f`` / ``$(mktemp)`` token in both the write and the
    reference — resolves to that in-command body instead of failing closed.

    ``fail_closed_body_file`` controls an UNREADABLE ``gh``/``glab`` body
    file: ``False`` (default, the quote scanner) keeps the #126 behaviour
    (an absent draft body contributes nothing); ``True`` (the
    destination-aware banned-terms / bare-reference gates) appends the
    fail-closed sentinel so a PUBLIC file-body post whose body the gate
    cannot read hard-blocks instead of slipping through unread.

    A ``git commit -F <relpath>`` body file is resolved against the dir whose
    repo the commit LANDS in (the command's own ``cd``/``-C``/``--git-dir``,
    via :func:`_body_file_resolution.commit_body_file_base`); a ``cd <dir> && gh
    pr create --body-file <relpath>`` body file is resolved against the command's
    leading ``cd`` dir (:func:`_body_file_resolution.command_body_file_base`);
    else the harness-provided ``cwd``. This handles the cold hook's reset cwd, so
    the gate scans the real body and applies the private-repo carve-out instead
    of fail-closing on a clean post whose body it could not read.
    """
    from teatree.hooks._body_file_resolution import (  # noqa: PLC0415
        BodyFileContext,
        command_body_file_base,
        commit_body_file_base,
        heredoc_files_map,
        unredirected_heredoc_bodies,
    )

    parts: list[str] = []
    tokens = tokenize(command)
    ctx = BodyFileContext(
        heredoc_files=heredoc_files_map(command, tokens),
        fail_closed_body_file=fail_closed_body_file,
        base=commit_body_file_base(command, cwd) or command_body_file_base(command) or cwd,
    )
    for segment in split_commands(tokens):
        _walk_command_segment(segment, parts, ctx)
    # Heredocs still need to be parsed against the raw command — the lexer treats
    # them as regular content since heredoc bodies live on subsequent physical
    # lines. Only heredocs fed straight to a CONSUMER (stdin / ``$(cat <<EOF)``)
    # are emitted here; a ``> path <<EOF`` heredoc writes to a file resolved by
    # path-pairing, so emitting it blanket would scan an unposted scratch body
    # and double-count a posted one.
    parts.extend(unredirected_heredoc_bodies(command))
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
