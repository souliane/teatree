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

import re
from typing import Final

from teatree.hooks._arg_body_walkers import (
    FAIL_CLOSED_SENTINEL,
    api_field_values,
    heredoc_file_bodies,
    heredoc_inline_bodies,
    is_fail_closed_sentinel,
    walk_api_fields,
    walk_body_file_flags,
    walk_body_flags,
    walk_curl_args,
)
from teatree.hooks._publish_detection import (
    command_has_opaque_forge_transport,
    command_has_token_aware_publish_surface,
    extract_title_fragments,
    segment_word_lists,
)
from teatree.hooks._shell_lexer import Token, TokenKind, is_command_separator, split_commands, tokenize

__all__ = [
    "FAIL_CLOSED_SENTINEL",
    "extract_bash_payload",
    "extract_secret_scan_text",
    "extract_title_fragments",
    "first_segment_words",
    "is_fail_closed_sentinel",
    "is_publish_command",
    "normalize_for_substring_match",
]

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
    walk_body_flags(words, payloads)
    walk_body_file_flags(
        words,
        payloads,
        leader=first,
        heredoc_files=heredoc_files,
        fail_closed_body_file=fail_closed_body_file,
    )
    # ``gh api`` / ``glab api`` field assignments.
    if first in {"gh", "glab"}:
        walk_api_fields(words, payloads)
    if first == "curl":
        walk_curl_args(words, payloads)


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
    heredoc_files = heredoc_file_bodies(command)
    tokens = tokenize(command)
    for segment in split_commands(tokens):
        _walk_command_segment(segment, parts, heredoc_files, fail_closed_body_file=fail_closed_body_file)
    # Heredocs still need to be parsed against the raw command — the
    # lexer treats them as regular content since heredoc bodies live on
    # subsequent physical lines. The matcher below tolerates that shape.
    parts.extend(heredoc_inline_bodies(command))
    # A forge call hidden inside an interpreter / wrapper argument
    # (``sh -c "gh ... --body X"``, ``eval``, ``ssh host gh``, ``xargs gh``)
    # carries its body in an opaque token the walkers cannot descend into; the
    # destination-aware gates fail closed on it so an unscannable public post
    # hard-blocks rather than slips through unread.
    if fail_closed_body_file and command_has_opaque_forge_transport(command):
        parts.append(FAIL_CLOSED_SENTINEL)
    return "\n".join(parts)


# ── Secret-scan surfaces (#1672) ────────────────────────────────────


def extract_secret_scan_text(command: str) -> str:
    """Concatenate EVERY surface a secret must be blocked on, regardless of destination.

    A secret leaks on ALL surfaces (a title, a short ``-t`` flag, a
    ``gh api -f title=`` field), not only the description body the carve-out
    is about. This widens the secret check beyond :func:`extract_bash_payload`
    to also cover the title / commit-subject fragments
    (:func:`extract_title_fragments`) and every ``gh``/``glab api`` field value
    (:func:`_arg_body_walkers.api_field_values`), so
    :func:`publish_surface.contains_secret` sees them before the destination
    skip can short-circuit a scan.
    """
    parts = [extract_bash_payload(command, fail_closed_body_file=False)]
    parts.extend(extract_title_fragments(command))
    for words in segment_word_lists(command):
        if words[0] in {"gh", "glab"}:
            parts.extend(api_field_values(words))
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
