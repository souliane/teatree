r"""Leaf primitives shared by the pre-publish command parser and its helpers.

Extracted from :mod:`teatree.hooks._command_parser` to break the genuine import
cycle it had with :mod:`teatree.hooks._body_file_resolution`: the body-file
resolver needs the fail-closed sentinels + the ``attached_value`` / ``read_file_arg``
helpers, but ``_command_parser`` also calls back into the resolver. Hosting the
shared primitives in this dependency-free LEAF lets both modules import DOWN into
it (one-directional) instead of reaching sideways into each other, so the lazy
call-time imports the cycle previously forced are no longer needed (#F7.9).

The module imports only the stdlib; it must never import another
``teatree.hooks`` module, so it stays a true leaf every parser module can depend
on. ``_command_parser`` re-exports every name here for backward compatibility, so
existing ``from teatree.hooks._command_parser import FAIL_CLOSED_SENTINEL`` (and
siblings) keep resolving unchanged.
"""

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


def attached_value(token: str, prefix: str) -> str | None:
    """Return the attached value of ``-X<value>`` / ``-X=<value>``, if any.

    Returns the substring AFTER ``prefix`` when ``token`` starts with the
    prefix and is strictly longer than it. ``-X=value`` strips the
    leading ``=`` so callers see the bare payload.
    """
    if token.startswith(prefix) and len(token) > len(prefix):
        return token[len(prefix) :].removeprefix("=")
    return None
