r"""Structural purity primitives for the publish-surface carve-out (#1657).

Split out of :mod:`teatree.hooks.publish_surface` to keep that module under
the module-health LOC cap. This module owns one concern: given a Bash command,
decide -- by STRUCTURE alone, without resolving repo visibility -- whether the
WHOLE command is provably a PURE ``gh``/``glab`` posting command and nothing
else.

The carve-out used to try to DETECT a hidden ``gh``/``glab``-to-public
invocation by enumerating transport mechanisms (shell ``-c``, ``env -S``,
here-string, ``eval``, pipe-to-shell, ...). That is an unbounded denylist:
every new shell construct that can run a command (``ssh host gh ...``,
``node -e "...gh..."``, ``make`` with a ``gh`` recipe, ...) leaks until it is
enumerated. Static analysis of "will this command, by ANY means, run ``gh``
against a public repo" is undecidable; enumeration cannot win.

This module INVERTS the model. Instead of "detect hidden bad", it PROVES the
command is ENTIRELY good and fails closed on anything it cannot prove. A
command is pure iff every top-level segment, after stripping a benign
``cd <path>`` / ``VAR=value`` prefix, is a recognised ``gh``/``glab`` posting
invocation in which EVERY token belongs to that invocation -- the verb triple,
a flag, a flag value, or a positional -- with no execution-transport or
ambiguity construct (pipe, redirection, here-doc/here-string, command or
process substitution, group/subshell opener) anywhere. A second non-``gh``
verb, ANY transport construct, or a token that is not part of the recognised
post makes the command not pure, so the carve-out fails closed (hard-block
stands). Repo-visibility resolution -- "is THIS post's ``--repo`` private" --
is layered on top in :mod:`teatree.hooks.publish_surface`; this module is the
visibility-independent structural half.

``command_segments`` (the WORD-value splitter shared with publish_surface) and
the inline-env-assignment regex live here because the purity proof is their
heaviest consumer; publish_surface re-imports both.
"""

import re
from typing import Final

from teatree.hooks._shell_lexer import TokenKind, split_commands, tokenize

# A leading ``KEY=value`` token is an inline env assignment, not the
# command name -- bash applies it to the command's environment. Skipped
# so ``FOO=1 gh issue create ...`` is still classified as a ``gh`` post.
ENV_ASSIGNMENT_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")

# Command words that open a recognised ``gh``/``glab`` posting invocation. The
# match is EXACT -- a path-form (``/usr/bin/gh``), an alias, or an opener-
# prefixed token (``(gh``, ``$(gh``) is NOT ``gh``/``glab`` and so cannot start
# a pure post.
_GH_GLAB_WORDS: Final[frozenset[str]] = frozenset({"gh", "glab"})

# Execution-transport / ambiguity markers that may appear ANYWHERE inside a
# single WORD token (the shell lexer keeps these attached to the surrounding
# word rather than emitting them as separators). Their presence in any token
# of a segment means the segment can run a second, unverifiable command
# (command substitution ``$(...)`` / backtick, process substitution
# ``<(...)`` / ``>(...)``), so the command is NOT a pure post and fails closed.
# A bare ``(`` in prose (``--body "see (gh issue 5)"``) is deliberately NOT
# here -- only the substitution-introducing two-char forms and the backtick.
_SUBSTITUTION_MARKERS: Final[tuple[str, ...]] = ("$(", "<(", ">(", "`")

# Transport operator/opener tokens. A token that IS one of these, or STARTS
# with a redirection operator, is an execution-transport construct (a
# redirection ``>``/``<``/``>>``, a here-doc/here-string ``<<``/``<<<``, or a
# group/subshell opener ``(``/``{``) -- never part of a recognised posting
# invocation's flags or positionals. A flag VALUE (the token consumed after a
# value-taking flag) is exempt from this opener check because a quoted body may
# legitimately start with such a character as prose; it is still subject to the
# substitution-marker check above.
_TRANSPORT_OPENERS: Final[frozenset[str]] = frozenset({"(", ")", "{", "}"})
_REDIRECTION_PREFIXES: Final[tuple[str, ...]] = (">", "<")

# The minimum word count of a posting invocation: ``<tool> <sub> <verb>``
# (e.g. ``gh pr create``). Fewer words cannot be a recognised post.
_POSTING_WORD_COUNT: Final[int] = 3

# A ``cd <path>`` prefix needs the verb plus its path argument.
_CD_WITH_PATH_WORD_COUNT: Final[int] = 2

# A bare short flag (``-R``) is exactly two characters; longer (``-Rx`` /
# ``-R=x``) carries an attached value, so the next token is not consumed.
_BARE_SHORT_FLAG_LEN: Final[int] = 2


def command_segments(command: str) -> list[list[str]]:
    """Return the WORD-value lists of every ``&&``/``;``/``|``/``&``/newline segment.

    Each segment's leading inline env assignments (``FOO=1 gh ...``) are
    stripped, mirroring :func:`publish_surface.is_git_commit_command`, so a
    posting verb behind an env prefix is still seen. Empty segments are dropped.

    The banned-terms SCANNER inspects the WHOLE payload (it finds a term in
    any segment), so the carve-out must inspect every segment too -- a
    posting verb behind a leading ``cd ... &&`` / env-assignment prefix is
    a true command, not noise, and ignoring it over-blocks a legitimate
    private-repo post.
    """
    segments: list[list[str]] = []
    for segment in split_commands(tokenize(command)):
        words = [tok.value for tok in segment if tok.kind is TokenKind.WORD]
        while words and ENV_ASSIGNMENT_RE.fullmatch(words[0]):
            words = words[1:]
        if words:
            segments.append(words)
    return segments


def strip_benign_prefix(words: list[str]) -> list[str] | None:
    """Strip a leading ``cd <path>`` and/or ``VAR=value`` prefix from ``words``.

    The prefix a pure post may legitimately carry is ONLY a ``cd <path>``
    (change the working directory the post runs from) and/or inline
    ``VAR=value`` env assignments (env assignments are already stripped by
    :func:`command_segments`, but a ``cd`` may sit between them and the verb).
    Returns the remaining words after the prefix, or ``None`` when the prefix
    is malformed (``cd`` with no path argument) -- a malformed prefix is not
    provably benign, so the segment cannot be a pure post.
    """
    rest = words
    while rest:
        if ENV_ASSIGNMENT_RE.fullmatch(rest[0]):
            rest = rest[1:]
            continue
        if rest[0] == "cd":
            if len(rest) < _CD_WITH_PATH_WORD_COUNT:
                return None
            rest = rest[_CD_WITH_PATH_WORD_COUNT:]
            continue
        break
    return rest


def token_has_substitution_marker(token: str) -> bool:
    """Return True iff ``token`` carries a command/process-substitution marker.

    These (``$(``, ``<(``, ``>(``, backtick) introduce a second command whose
    target the gate cannot resolve, so a post carrying one is not provably
    pure. Applied to EVERY token -- flags, flag values, and positionals --
    because a substitution can hide inside a quoted ``--body`` value
    (``--body "$(gh ... --repo PUBLIC ...)"``) just as easily as a bare token.
    """
    return any(marker in token for marker in _SUBSTITUTION_MARKERS)


def token_is_transport_construct(token: str) -> bool:
    """Return True iff ``token`` IS a redirection/here-doc/group-opener construct.

    A standalone group/subshell opener (``(``, ``{``, ``)``, ``}``) or a token
    starting with a redirection operator (``>``, ``<``, including ``>>`` /
    ``<<`` / ``<<<``) is execution transport, never part of a recognised
    posting invocation. Checked for non-value tokens only (a quoted flag VALUE
    may legitimately start with such a character as prose).
    """
    return token in _TRANSPORT_OPENERS or token.startswith(_REDIRECTION_PREFIXES)


def token_is_redirect_operator(token: str) -> bool:
    """Return True iff ``token`` starts a redirection/here-doc (not a group opener).

    The redirect half of :func:`token_is_transport_construct`: a token starting
    with ``>``/``<`` (``>``, ``>>``, ``>|``, ``<``, ``<<``, ``<<<``), whether
    bare (``> file``) or glued (``>file``). A bare group/subshell opener
    (``(``/``{``/``)``/``}``) is excluded -- it is transport but not a redirect,
    so it has no local-file target to classify.
    """
    return token.startswith(_REDIRECTION_PREFIXES)


def _value_taking_flag(token: str) -> bool:
    """Return True iff ``token`` is a flag whose value is the NEXT token.

    A long flag in equals form (``--repo=X``) or an attached short flag
    (``-Rx``) carries its own value, so the next token is NOT consumed. A
    bare long flag (``--repo``) or bare short flag (``-R``) consumes the next
    token as its value. This is the standard gh/glab/POSIX flag grammar and
    needs no per-flag enumeration -- the purity proof only needs to know which
    tokens are opaque VALUES (substitution-checked only) versus structural
    tokens (transport-checked too).
    """
    if not token.startswith("-"):
        return False
    if token.startswith("--"):
        return "=" not in token
    # Short flag: ``-R`` takes the next token; ``-Rx`` / ``-R=x`` is attached.
    return len(token) == _BARE_SHORT_FLAG_LEN


def segment_is_pure_gh_glab_post(words: list[str]) -> bool:
    r"""Return True iff ``words`` is, by STRUCTURE, a pure ``gh``/``glab`` post.

    "Pure" means: after stripping a benign ``cd <path>`` / ``VAR=value``
    prefix, the segment is a recognised ``gh``/``glab`` posting invocation
    (``words[0]`` EXACTLY ``gh``/``glab``, a recognised posting subcommand --
    delegated to the caller via :func:`publish_surface._segment_is_posting_verb`)
    in which EVERY remaining token is a benign part of that invocation:

    - the verb triple (``gh pr create``, ``glab mr note``);
    - a flag (``--repo``, ``-R``, ``--title``, ``--body``, ``--label``, ...);
    - the opaque VALUE consumed by a value-taking flag -- allowed to contain
        arbitrary prose (the word ``gh``, a quoted ``sh -c ...`` string) but
        NOT a command/process-substitution marker (``$(``, backtick, ``<(``,
        ``>(``), which would run a second unverifiable command at runtime; or
    - a positional argument (an issue/PR number) -- subject to BOTH the
        substitution-marker check and the transport-construct check.

    ANY token that is not classifiable as one of the above -- a second
    non-``gh`` verb, a pipe/redirection/here-doc/here-string operator, a
    group/subshell opener, a token whose basename is a shell or interpreter
    (these are simply not ``gh``/``glab`` flags or positionals, so they fail
    the positive check; they are NOT enumerated) -- makes the segment NOT a
    pure post. This is the anti-whack-a-mole inversion: the proof is a closed
    POSITIVE classification of every token, so an un-enumerated transport
    cannot leak -- it is rejected for not being part of the recognised post.

    NOTE: this is the STRUCTURAL half only. The posting-subcommand recognition
    and the repo-visibility (``--repo`` private?) layer live in
    :mod:`teatree.hooks.publish_surface`, which calls this after confirming the
    segment is a recognised posting verb; here ``words[0] in {gh, glab}`` and
    the per-token classification are the structural invariants.
    """
    rest = strip_benign_prefix(words)
    if rest is None or len(rest) < _POSTING_WORD_COUNT:
        return False
    if rest[0] not in _GH_GLAB_WORDS:
        return False
    expect_value = False
    for token in rest[_POSTING_WORD_COUNT:]:
        if token_has_substitution_marker(token):
            return False
        if expect_value:
            expect_value = False
            continue
        if token_is_transport_construct(token):
            return False
        expect_value = _value_taking_flag(token)
    return True
