"""Always-allowed self-rescue commands (NEVER-LOCKOUT contract).

A gate that can deny the very command an operator uses to disable it is a
deadlock — the factory has wedged itself twice on exactly this
(souliane/teatree#1472, #1474). This module names the small, fixed set of
commands EVERY gate and EVERY hook must let through unconditionally, no
matter how a gate's detection misbehaves:

- DB migrate (``t3 <overlay> db migrate`` / ``worktree provision`` /
    ``manage.py migrate``): bring a wedged schema forward so the rest of the
    CLI (and the gates that shell it) work again.
- Gate disables (``t3 <overlay> gate disable``,
    ``t3 <overlay> gate skill-loading disable``,
    ``t3 <overlay> gate config-overwrite disable``,
    ``t3 <overlay> gate main-clone disable``): the orchestrator-Bash,
    skill-loading-on-task, config-overwrite, and main-clone kill-switches
    (#1474, #2836) — each must reach its own disable even if its gate
    misdetects.
- The fail-open toggle (``t3 <overlay> gate fail-open enable``): the master
    switch that flips every over-deny gate to fail-open at once.

:func:`is_self_rescue` is pure detection over EVERY shell segment of the
command (lexed via the shared :mod:`teatree.hooks._shell_lexer`, the same
splitter the hook-router blocklist scans). The call rescues only when
*every* segment is itself a self-rescue command, so a self-rescue prefix
glued to a second command through a shell separator
(``&& ; || |`` / newline) can never smuggle a blocked command past a gate
— the chained command is its own non-self-rescue segment and rejects the
whole call. A segment carrying a command substitution (``$(...)`` /
backticks) or process substitution (``<(...)`` / ``>(...)``) is likewise
never a rescue: it embeds an arbitrary command we cannot vet.

The per-segment match is STRICT, CONTIGUOUS, and POSITIONAL. Each entry is
a fixed sequence of literal tokens plus at most one ``OVERLAY`` wildcard
slot that consumes EXACTLY ONE token (the overlay name between ``t3`` and
the verb). After stripping a legitimate leading ``KEY=val`` env-assignment
prefix (and, for the ``manage.py`` entry only, a single leading
``python``/``python3`` interpreter token), a segment matches an entry iff:

1. the segment's tokens map ONE-TO-ONE and IN ORDER onto the entry's
    tokens — ``entry[0]`` is ``argv[0]``, the ``OVERLAY`` slot eats one
    token, every other entry token equals the segment token at the same
    position. NO arbitrary token may appear BETWEEN entry tokens; and
2. every REMAINING segment token after the entry satisfies a CLOSED
    trailing-token policy — it is a boolean flag (``--name`` / ``-x``), a
    glued value-flag (``--name=value``, value a plain word), or a recognised
    value-flag plus its plain-word value (``--reason cleanup``). A bare
    positional (a command word, a URL, a path, a lone ``-``/``--``), an
    operator/redirect token, or a glued value carrying an operator rejects
    the segment. It is an allow-list of flag SHAPES, not a permissive
    "any ``--``-prefixed token" catch-all.

So ``t3 acme gate disable`` and ``t3 acme gate disable --yes`` match, but
``t3 acme git push gate disable`` (positional ``git push`` between the
overlay and the verb) and ``t3 acme gate disable git push`` (trailing
positional) do NOT. The ``env`` command is NOT a rescue — only a bare
unquoted ``KEY=val`` assignment is stripped.

DESIGN: self-rescue matching intentionally does NOT support I/O redirects.
A rescue is the bare command plus flags — a redirect is never needed to
rescue a lockout — so ANY redirect operator in a segment (``>`` / ``>>`` /
``<`` / ``2>&1`` / ``&>`` / attached-target / a redirect in a flag-value
position) makes the segment a non-rescue. Rejecting redirects cannot lock
anyone out (the operator runs the bare command, which always rescues) and
is strictly SAFER, while keeping this security matcher free of
shell-redirect-grammar parsing.

The env-prefix strip is QUOTE-ACCURATE: it recognises an assignment from
the token's RAW source, so it matches the shell. ``FOO=bar t3 …`` strips,
but ``'A'=1 t3 …`` / ``A''=1 t3 …`` do not — the shell treats those as a
command literally named ``A=1`` (a quoted name is not an assignment), so
the pseudo-assignment becomes ``argv[0]`` and the segment correctly fails
to match any rescue. A quoted ``argv[0]`` that DECODES to a genuine rescue
command (``'t3' acme gate disable``) still rescues, because the shell runs
it as ``t3 acme gate disable``.
"""

import re
from typing import Final

from teatree.hooks._shell_lexer import Token, TokenKind, split_commands, tokenize


class _OverlaySlot:
    """Wildcard slot in a self-rescue entry: consumes EXACTLY ONE token.

    Sits between ``t3`` and the verb to absorb the overlay name (``acme`` /
    ``t3-teatree`` / ``review`` / …), so a single entry covers every
    overlay without permitting arbitrary intervening tokens.
    """


OVERLAY: Final[_OverlaySlot] = _OverlaySlot()

# Each entry is a strict, contiguous token pattern: literal strings matched
# positionally, with the single ``OVERLAY`` wildcard consuming exactly one
# token. ``gate disable`` and ``gate skill-loading disable`` are DISTINCT
# entries — the skill-loading kill-switch is its own three-verb path, never
# ``gate disable`` with an interior token skipped. ``manage.py migrate`` is
# the raw-Django escape for a wedged DB (run directly or via an interpreter).
_EntryToken = str | _OverlaySlot
SELF_RESCUE_ALLOWLIST: Final[tuple[tuple[_EntryToken, ...], ...]] = (
    ("t3", OVERLAY, "db", "migrate"),
    ("t3", OVERLAY, "worktree", "provision"),
    ("t3", OVERLAY, "gate", "disable"),
    ("t3", OVERLAY, "gate", "skill-loading", "disable"),
    ("t3", OVERLAY, "gate", "config-overwrite", "disable"),
    ("t3", OVERLAY, "gate", "main-clone", "disable"),
    ("t3", OVERLAY, "gate", "fail-open", "enable"),
    ("manage.py", "migrate"),
)


# A leading shell env-assignment prefix (``FOO=bar t3 …`` and the append
# form ``FOO+=bar t3 …``). Bash applies these to the command's environment;
# the real ``argv[0]`` is the first NON-assignment token. Matched against
# the WHOLE RAW token (quotes intact), NOT its decoded value, because the
# shell recognises an assignment ONLY when the name and ``=`` are UNQUOTED:
# ``'A'=1`` / ``A''=1`` decode to ``A=1`` but the shell runs them as a
# command LITERALLY named ``A=1`` (no assignment), so they must NOT be
# stripped. The name + optional ``+`` + ``=`` must be bare identifier
# characters AND the VALUE must be a single plain word — no shell
# redirect/operator characters (``< > & | ;``) and no further ``=`` (a
# value carrying another ``=`` is not a clean assignment: ``FOO=bar=baz``
# must not be stripped). Anything failing this falls through unstripped,
# becomes ``argv[0]``, and the segment fails to match a rescue. (``$(`` /
# backtick / ``<(`` substitution in a value is rejected upstream by
# :func:`_has_command_substitution`.) The ``env`` *command* is deliberately
# NOT covered — ``env t3 gate disable`` is a different program and is not a
# rescue.
_ENV_ASSIGNMENT_RAW_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=[^<>&|;=]*$")

# Interpreters that run ``manage.py`` as their first script argument. Only
# the ``manage.py`` entry is normally invoked indirectly, so one leading
# interpreter token immediately before ``manage.py`` is tolerated.
_MANAGE_PY_INTERPRETERS: Final[frozenset[str]] = frozenset({"python", "python3"})
_MANAGE_PY_NAMES: Final[frozenset[str]] = frozenset({"manage.py", "./manage.py"})

# Command-embedding markers the lexer leaves inside a WORD token rather than
# splitting into their own segment: command substitution (``$(...)`` /
# backticks) and process substitution (``<(...)`` / ``>(...)``). Each runs
# an arbitrary command, so a segment carrying one is never a rescue. Plain
# redirects (``>`` / ``>>`` / ``<``) lex as a standalone operator-or-path
# word and so are NOT markers — only the ``(``-attached form is.
_COMMAND_EMBED_MARKERS: Final[tuple[str, ...]] = ("$(", "`", "<(", ">(")

# Trailing flags whose value is a SEPARATE next token (``--reason why``);
# the value token is allowed to be a bare positional ONLY because it is the
# argument of a recognised value-flag, AND only when that value is a PLAIN
# word (not a flag, not a redirect operator). Self-rescue commands take no
# positional arguments, so the allowed value-flags are deliberately few.
_VALUE_FLAGS: Final[frozenset[str]] = frozenset({"--reason"})

# A real CLI flag: one or two leading dashes then at least one non-dash
# character (``-x`` / ``--reason`` / ``--no-input``). A lone ``-`` / ``--``
# is a positional, not a flag, so it must NOT pass the trailing-token check.
_FLAG_RE: Final[re.Pattern[str]] = re.compile(r"^-{1,2}[^-]")

# A complete BOOLEAN long/short flag with NO glued value: dash(es), then a
# flag name of word characters (and internal dashes, ``--no-input``). No
# ``=``, no operator/redirect characters. ``--yes`` / ``-x`` / ``--no-input``.
_BOOLEAN_FLAG_RE: Final[re.Pattern[str]] = re.compile(r"^-{1,2}[A-Za-z0-9][A-Za-z0-9-]*$")

# A glued value-flag ``--name=value``: a long-flag name then ``=`` then a
# value that is a PLAIN word — no shell operator / redirect / substitution
# characters (``< > & | ; $ `` ( ) `` and whitespace). An EMPTY value
# (``--reason=``) is allowed (it is ``--reason ''``). The value is checked
# for the FULL operator-char class so the closed policy rejects a smuggled
# operator even if the lexer had not already split it into its own token.
_GLUED_VALUE_FLAG_RE: Final[re.Pattern[str]] = re.compile(r"^--[A-Za-z0-9][A-Za-z0-9-]*=[^<>&|;$`()\s]*$")

# An I/O redirect operator RUN, recognised ANYWHERE inside a word so a
# redirect glued to an adjacent word (``FOO=>``, ``--reason FOO=>``) is
# split off as its own token by :func:`_split_operators`. Covers bare ``>``
# / ``>>`` / ``<`` / ``<<`` / ``<<<`` / ``<>``, fd-prefixed (``2>`` …),
# combined (``&>`` / ``&>>``), and fd-duplication tails (``2>&1`` / ``>&-``).
# Longest alternations first so ``>>`` is not split as two ``>``.
_REDIRECT_OPERATOR_RUN_RE: Final[re.Pattern[str]] = re.compile(
    r"&>>|&>|\d*(?:>>|<<<|<<|<>|>|<)(?:&\d*-?)?",
)

# A token that IS exactly a redirect operator (used after operator-splitting
# to reject any redirect token). Self-rescue matching does NOT support
# redirects by design (see the module docstring), so a redirect token
# anywhere — including a flag-value position — makes the segment a non-rescue.
_REDIRECT_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"^(?:&>>|&>|\d*(?:>>?|<<?<?|<>)(?:&\d*-?)?)$")


def _split_operators(word: str) -> list[str]:
    """Split a decoded word into shell sub-tokens at embedded redirect runs.

    The shared lexer leaves a redirect operator glued to an adjacent word
    (``FOO=>`` stays one WORD token), which let a redirect hide from the
    operator-aware rejection. This re-splits a word so every redirect
    operator is its OWN token — ``FOO=>`` → ``["FOO=", ">"]`` — making the
    matcher operate on proper shell tokens instead of regex-on-raw. A word
    with no redirect run returns ``[word]`` unchanged. Empty fragments
    between adjacent operators are dropped.
    """
    out: list[str] = []
    pos = 0
    for match in _REDIRECT_OPERATOR_RUN_RE.finditer(word):
        if match.start() > pos:
            out.append(word[pos : match.start()])
        out.append(match.group())
        pos = match.end()
    if pos < len(word):
        out.append(word[pos:])
    return out or [word]


def _has_command_substitution(tokens: list[Token]) -> bool:
    """True iff any token embeds a command/process substitution.

    Both the decoded value and the RAW source are inspected so a marker that
    survives only in one form (e.g. inside double quotes) still disqualifies
    the segment.
    """
    return any(marker in tok.value or marker in tok.raw for tok in tokens for marker in _COMMAND_EMBED_MARKERS)


def _strip_env_prefix(tokens: list[Token]) -> list[Token]:
    """Drop a leading run of UNQUOTED ``KEY=val`` env-assignment tokens.

    Recognition follows the shell: a token is an assignment only when its
    RAW source (quotes intact) begins with a bare ``IDENT=`` — the name and
    ``=`` are unquoted. ``FOO=bar`` strips; ``'A'=1`` / ``A''=1`` (which
    decode to ``A=1`` but the shell runs as a command literally named
    ``A=1``) do NOT strip, so that token becomes ``argv[0]`` and correctly
    fails to match any rescue entry.
    """
    i = 0
    while i < len(tokens) and _ENV_ASSIGNMENT_RAW_RE.match(tokens[i].raw):
        i += 1
    return tokens[i:]


def _strip_manage_py_interpreter(words: list[str]) -> list[str]:
    """Drop a single ``python``/``python3`` token when it leads ``manage.py``.

    ``manage.py`` is normally run through an interpreter, so
    ``python manage.py migrate`` must reduce to ``manage.py migrate``. The
    interpreter token is consumed ONLY when ``manage.py`` is its immediate
    next token (``argv[1]``) — never when ``manage.py`` is a later argument
    (``python -c pass manage.py migrate`` keeps the ``python`` head and so
    fails to match any entry).
    """
    if words[:1] and words[0] in _MANAGE_PY_INTERPRETERS and words[1:2] and words[1] in _MANAGE_PY_NAMES:
        return words[1:]
    return words


def _consume_entry(entry: tuple[_EntryToken, ...], words: list[str]) -> int | None:
    """Return the count of leading ``words`` the entry consumes, or ``None``.

    Matches the entry tokens CONTIGUOUSLY and POSITIONALLY from the start of
    ``words``: a literal token must equal the word at the same index; the
    ``OVERLAY`` slot consumes exactly one (any) token. Returns the number of
    consumed words on a full match, else ``None``. The ``manage.py`` literal
    also matches ``./manage.py`` so a direct ``./manage.py migrate`` rescues.
    """
    i = 0
    for spec in entry:
        if i >= len(words):
            return None
        if isinstance(spec, _OverlaySlot):
            i += 1
            continue
        if spec == "manage.py":
            if words[i] not in _MANAGE_PY_NAMES:
                return None
        elif words[i] != spec:
            return None
        i += 1
    return i


def _trailing_tokens_are_flags_only(trailing: list[str]) -> bool:
    """True iff every trailing token satisfies the CLOSED trailing-token policy.

    A self-rescue command takes no positional arguments. After the matched
    entry, each trailing token is allowed IFF it is exactly one of:

    (a) a BOOLEAN long/short flag — ``--name`` / ``-x`` — whose name is plain
        word characters with no ``=`` and no operator/redirect characters;
    (b) a VALUE-FLAG in either form — ``--reason cleanup`` (a recognised
        value-flag followed by a PLAIN-word value token) or glued
        ``--name=value`` where ``value`` is a plain word with NO operator /
        redirect / substitution characters (an empty ``--name=`` is allowed).

    ANYTHING ELSE rejects: a bare positional (``foo`` / a lone ``-`` / ``--``),
    any operator or redirect token, a command/process substitution, or a glued
    ``--name=value`` whose value carries an operator. This is a closed
    allow-list, not a permissive ``any --``-prefixed-token catch-all: a token
    must affirmatively match a recognised flag SHAPE, so a fragile edge cannot
    ride in on the prefix.

    I/O redirects stay unsupported by design (see the module docstring): the
    operator-aware tokenization splits a redirect into its own token, which
    matches none of (a)/(b) and so rejects.
    """
    i = 0
    n = len(trailing)
    while i < n:
        token = trailing[i]
        if _GLUED_VALUE_FLAG_RE.match(token):
            i += 1
            continue
        if not _BOOLEAN_FLAG_RE.match(token):
            return False  # bare positional, lone ``-``/``--``, redirect, or operator
        if token in _VALUE_FLAGS:
            value = trailing[i + 1] if i + 1 < n else None
            # A value-flag consumes its value ONLY when a PLAIN word follows
            # (not a flag/lone-dash, not a redirect/operator token). Otherwise
            # the flag stands alone and the next token is judged on its own.
            if value is not None and _is_plain_value_word(value):
                i += 2
                continue
        i += 1
    return True


def _is_plain_value_word(token: str) -> bool:
    """True iff ``token`` is a PLAIN value word a value-flag may consume.

    A value-flag's separate value (``--reason cleanup``) must be an ordinary
    word — not another flag, not a lone ``-``/``--``, not a redirect or
    operator token. Operator/substitution characters are rejected upstream
    (operator-split + substitution check), so the remaining disqualifiers are
    a flag-shaped token or a redirect token.
    """
    return not _FLAG_RE.match(token) and not _REDIRECT_TOKEN_RE.match(token)


def _segment_matches_entry(entry: tuple[_EntryToken, ...], cmd_words: list[str]) -> bool:
    """True iff ``cmd_words`` is exactly ``entry`` plus trailing flags only."""
    consumed = _consume_entry(entry, cmd_words)
    if consumed is None:
        return False
    return _trailing_tokens_are_flags_only(cmd_words[consumed:])


def _segment_is_self_rescue(tokens: list[Token]) -> bool:
    """True iff a single command segment is itself a self-rescue command.

    The match is STRICT: anchored at ``argv[0]`` (after stripping an UNQUOTED
    ``KEY=val`` env-assignment prefix and, for ``manage.py``, a leading
    interpreter), contiguous and positional over the entry tokens, with only
    trailing flags permitted afterwards. A blocked command can neither lead
    the segment, sit between the entry tokens, nor trail them as a bare
    positional. The env-prefix strip is quote-aware (operates on the raw
    source), so a quoted pseudo-assignment becomes ``argv[0]`` and rejects.

    After the env strip, the remaining tokens are reduced to their decoded
    VALUES: the shell runs ``'t3' acme gate disable`` as ``t3 acme gate
    disable``, so a quoted ``argv[0]`` that decodes to a genuine rescue
    command is a genuine rescue.
    """
    if not tokens or _has_command_substitution(tokens):
        return False
    # Operator-aware tokenization: env-strip first (raw, quote-accurate),
    # then split any redirect operator the shared lexer left glued to a word
    # into its own token, so the matcher sees proper shell tokens.
    words = [piece for tok in _strip_env_prefix(tokens) for piece in _split_operators(tok.value)]
    cmd_words = _strip_manage_py_interpreter(words)
    return any(_segment_matches_entry(entry, cmd_words) for entry in SELF_RESCUE_ALLOWLIST)


def is_self_rescue(command: str) -> bool:
    """Return True iff EVERY shell segment of ``command`` is a self-rescue command.

    The full command is lexed and split into segments with the same
    :func:`split_commands` the hook-router blocklist scans, then *every*
    segment must independently be a self-rescue command (strict contiguous
    positional match against an allowlist entry, trailing flags only). A
    self-rescue phrase glued to a second command via a shell separator
    (``&& ; || |`` / newline) is rejected because the second command is its
    own non-self-rescue segment; a self-rescue command carrying an embedded
    command/process substitution is rejected because the embedded command
    cannot be vetted; a blocked command cannot smuggle the rescue tokens as
    leading, interior, or trailing positionals within a single segment. A
    match means NO gate and NO hook may deny this call — it is the
    operator's guaranteed escape from a lockout — but only when the WHOLE
    call is rescue and nothing else.
    """
    if not command:
        return False
    segments = split_commands(tokenize(command))
    if not segments:
        return False
    return all(_segment_is_self_rescue([tok for tok in segment if tok.kind is TokenKind.WORD]) for segment in segments)
