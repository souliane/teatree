r"""Bash-like shell tokenizer for the quote-scanner gate (#1213).

A small but accurate-enough lexer that returns the same logical tokens
``bash`` would pass to a child process. Built specifically to close the
codex round-3 bypass paths that defeated the previous regex spot-check
approach:

- ``\<NL>`` is removed token-INTERNALLY (rejoins a token split across
    physical lines into the original word) and treated as whitespace
    BETWEEN tokens.
- ``;``, ``|``, ``&``, ``&&``, ``||`` and ``\n`` are emitted as standalone
    metacharacter tokens regardless of surrounding whitespace, so
    ``cmd "x";echo --quote-ok`` is split at the ``;`` even with no space.
- ANSI-C ``$'...'`` is decoded directly per the bash man-page (handling
    ``\\``, ``\'``, ``\"``, control letters, ``\xHH``, ``\uHHHH``,
    ``\UHHHHHHHH``, ``\nnn`` octal, ``\cX`` control) — no round-trip
    through ``unicode_escape`` and no shell-requoting that an escaped
    single quote could truncate.

The returned :class:`Token` carries the decoded VALUE plus the SHAPE
(``WORD`` vs ``OP``) so callers can route operator boundaries and
attached short options (``-d'{...}'``) correctly.
"""

import string
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Final


class TokenKind(Enum):
    """Whether a token is a regular word or a shell metacharacter."""

    WORD = "word"
    OP = "op"


@dataclass(frozen=True)
class Token:
    """One decoded shell token.

    ``value`` is the shell-decoded text (quotes removed, escapes applied);
    ``raw`` is the verbatim source span the token was lexed from (quotes
    and escapes intact). ``raw`` lets a consumer apply shell rules that
    depend on quoting — e.g. recognising a leading ``NAME=val`` env
    assignment only when the name and ``=`` are UNQUOTED (``'A'=1`` is a
    command literal, not an assignment). ``raw`` defaults to ``""`` so
    existing in-process constructions stay valid.
    """

    value: str
    kind: TokenKind
    raw: str = ""


def raw_substitution_sees_live(raw: str, openers: tuple[str, ...]) -> bool:
    """Quote-aware walk: True iff any ``openers`` marker in ``raw`` is LIVE.

    A command/process substitution (``$(...)``, ``<(...)``, ``>(...)``) or a
    backtick command substitution is expanded by bash only when it is unquoted
    or inside DOUBLE quotes; inside SINGLE quotes it is inert literal text bash
    passes verbatim. Both pre-publish raw-span walkers share this state machine
    so they classify a substitution identically. It tracks ``in_single`` and
    ``in_double`` over the verbatim source span:

    - A ``'`` toggles single-quote state ONLY when not inside a double-quoted
        span -- inside ``"..."`` an apostrophe is a LITERAL character, not a
        delimiter (the fail-open bug this fixes: ``"it's $(cat secret)"`` is one
        double-quoted string whose ``'`` is a literal apostrophe, so the
        ``$(...)`` after it is genuinely LIVE, not inert).
    - A ``"`` toggles double-quote state ONLY when not inside a single-quoted
        span -- inside ``'...'`` a double quote is a literal character (bash has
        no single-quote escape).
    - A marker in ``openers`` is LIVE the moment it opens while NOT inside a
        single-quoted region (unquoted OR double-quoted, both of which bash
        expands); INERT only inside a genuinely single-quoted span.

    Returns True on the first live marker, else False. Markers are matched as
    literal prefixes, so a single-character backtick opener composes with the
    two-character ``$(`` / ``<(`` / ``>(`` family.
    """
    in_single = False
    in_double = False
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == "'" and not in_double:
            in_single = not in_single
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            i += 1
            continue
        if not in_single:
            for opener in openers:
                if raw.startswith(opener, i):
                    return True
        i += 1
    return False


# Metacharacters that act as command separators in bash. Two-char
# operators MUST be checked before their one-char prefixes so ``&&`` is
# not mis-emitted as two ``&`` tokens.
_TWO_CHAR_OPS: Final[tuple[str, ...]] = ("&&", "||")
_ONE_CHAR_OPS: Final[tuple[str, ...]] = (";", "|", "&", "\n")

# Whitespace / newline character sets used throughout the lexer. Using
# set literals so membership checks are O(1) and ruff's PLR6201 is happy.
_INLINE_WHITESPACE: Final[frozenset[str]] = frozenset({" ", "\t"})
_NEWLINE_CHARS: Final[frozenset[str]] = frozenset({"\n", "\r"})
_DOUBLE_QUOTE_ESCAPES: Final[frozenset[str]] = frozenset({'"', "\\", "`", "$", "\n"})

# Heredoc redirect operators, LONGEST FIRST so ``<<-`` is recognised before its
# ``<<`` prefix. Emitted as ordinary WORD tokens (this lexer classifies
# ``<``/``>`` textually downstream -- see ``token_is_transport_construct`` in
# ``_gh_glab_hiding.py`` -- rather than as lexer-level operators), so heredoc
# detection happens post-hoc on the flushed WORD value in ``_note_heredoc_word``.
_HEREDOC_OPS: Final[tuple[str, str]] = ("<<-", "<<")


# ANSI-C escape decoding table for ``$'...'`` per bash man-page.
_ANSI_C_SIMPLE_ESCAPES: Final[dict[str, str]] = {
    "a": "\a",
    "b": "\b",
    "e": "\x1b",
    "E": "\x1b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "?": "?",
}


def _read_digits(literal: str, start: int, max_len: int, alphabet: str) -> str:
    """Return the longest prefix of ``literal[start:]`` over ``alphabet``."""
    end = start
    n = len(literal)
    limit = min(start + max_len, n)
    while end < limit and literal[end] in alphabet:
        end += 1
    return literal[start:end]


def _decode_ansi_c_hex(literal: str, start: int, max_len: int) -> tuple[str, int]:
    r"""Decode ``\xHH`` / ``\uHHHH`` / ``\UHHHHHHHH`` at ``start``.

    Returns the decoded character plus the index ONE PAST the last
    consumed digit. If no hex digits follow, returns the marker letter
    so the caller can emit it literally.
    """
    digits = _read_digits(literal, start, max_len, string.hexdigits)
    if not digits:
        # No digits — caller emits the marker letter (the char at
        # ``start - 1``) literally.
        return literal[start - 1], start
    try:
        return chr(int(digits, 16)), start + len(digits)
    except ValueError:
        return literal[start - 1], start + len(digits)


def _decode_ansi_c_octal(literal: str, start: int) -> tuple[str, int]:
    r"""Decode ``\nnn`` octal at ``start`` (caller positioned ON first digit)."""
    digits = _read_digits(literal, start, 3, string.octdigits)
    return chr(int(digits, 8) & 0xFF), start + len(digits)


_ANSI_C_HEX_WIDTH: Final[dict[str, int]] = {"x": 2, "u": 4, "U": 8}


def _decode_ansi_c_escape(literal: str, i: int) -> tuple[str, int]:
    r"""Decode one escape sequence starting at ``literal[i] == '\'``.

    Returns the decoded text plus the next-cursor index. The caller is
    responsible for the loop; this helper handles ONE escape per call.
    """
    n = len(literal)
    if i + 1 >= n:
        return literal[i], i + 1
    nxt = literal[i + 1]
    if nxt in _ANSI_C_SIMPLE_ESCAPES:
        return _ANSI_C_SIMPLE_ESCAPES[nxt], i + 2
    hex_width = _ANSI_C_HEX_WIDTH.get(nxt)
    if hex_width is not None:
        return _decode_ansi_c_hex(literal, i + 2, hex_width)
    if nxt in string.octdigits:
        return _decode_ansi_c_octal(literal, i + 1)
    if nxt == "c" and i + 2 < n:
        return chr(ord(literal[i + 2].upper()) ^ 0x40), i + 3
    # Unknown escape — bash echoes the backslash + next char.
    return literal[i : i + 2], i + 2


def _decode_ansi_c(literal: str) -> str:
    r"""Decode a stripped ``$'...'`` body per the bash man-page.

    The literal is the text BETWEEN the opening ``$'`` and closing
    ``'`` — caller is responsible for slicing. We walk character by
    character so an escaped single quote (``\'``) does not terminate
    the string early; the closing quote is detected during tokenization
    BEFORE this decoder runs.
    """
    out: list[str] = []
    i = 0
    n = len(literal)
    while i < n:
        if literal[i] != "\\":
            out.append(literal[i])
            i += 1
            continue
        decoded, i = _decode_ansi_c_escape(literal, i)
        out.append(decoded)
    return "".join(out)


def _heredoc_delimiter_from_glued_token(value: str) -> tuple[str, bool] | None:
    """Return ``(delimiter, strip_tabs)`` iff ``value`` is a GLUED heredoc token.

    ``<<EOF`` / ``<<-EOF`` / ``<<'EOF'`` / ``<<"EOF"`` all fuse the operator and
    delimiter into ONE flushed word -- a quote consumed immediately after ``<<``
    never flushes mid-token (:meth:`_LexerState.begin_token` only records a NEW
    start when no token is already open), so the decoded delimiter text lands in
    the SAME token as the operator prefix. Returns ``None`` for the bare
    space-separated operator alone (``value in _HEREDOC_OPS`` with nothing
    trailing) or a value with no heredoc-op prefix at all.
    """
    for op in _HEREDOC_OPS:
        if value.startswith(op) and len(value) > len(op):
            return value[len(op) :], op == "<<-"
    return None


@dataclass
class _LexerState:
    """Mutable state shared by the lexer's character handlers."""

    command: str
    tokens: list[Token]
    current: list[str]
    in_token: bool
    i: int
    token_start: int = 0
    pending_heredocs: list[tuple[str, bool]] = field(default_factory=list)
    _await_heredoc_delim: bool = False
    _await_heredoc_strip_tabs: bool = False

    def begin_token(self) -> None:
        """Record the raw-source start index when a fresh token opens."""
        if not self.in_token:
            self.token_start = self.i
        self.in_token = True

    def flush(self) -> None:
        if self.in_token:
            raw = self.command[self.token_start : self.i]
            value = "".join(self.current)
            self.tokens.append(Token(value, TokenKind.WORD, raw=raw))
            self.current.clear()
            self.in_token = False
            self._note_heredoc_word(value)

    def _note_heredoc_word(self, value: str) -> None:
        """Queue a heredoc body for consumption if ``value`` opens or completes one.

        A heredoc redirect (``<<``/``<<-``) is tokenized as an ordinary WORD (this
        lexer classifies ``<``/``>`` textually downstream, not as lexer-level
        operators — see :func:`token_is_transport_construct`), so the delimiter
        must be recognised across TWO shapes: space-separated (``<< 'EOF'`` —
        this word IS the bare operator, the delimiter is the NEXT flushed word) or
        glued (``<<EOF`` / ``<<-EOF`` / ``<<'EOF'`` — quote consumption never
        flushes mid-token, so operator and delimiter fuse into one word).
        """
        if self._await_heredoc_delim:
            self._await_heredoc_delim = False
            self.pending_heredocs.append((value, self._await_heredoc_strip_tabs))
            return
        glued = _heredoc_delimiter_from_glued_token(value)
        if glued is not None:
            self.pending_heredocs.append(glued)
            return
        if value in _HEREDOC_OPS:
            self._await_heredoc_delim = True
            self._await_heredoc_strip_tabs = value == "<<-"

    def append_op(self, op: str, consumed: int) -> None:
        self.flush()
        self.tokens.append(Token(op, TokenKind.OP, raw=op))
        self.i += consumed


def _consume_line_continuation(state: _LexerState) -> None:
    r"""Skip ``\<NL>``. Between-token: separator. In-token: rejoin."""
    j = state.i + 1
    if state.command[j] == "\r" and j + 1 < len(state.command) and state.command[j + 1] == "\n":
        state.i = j + 2
    else:
        state.i = j + 1


def _consume_pending_heredocs(state: _LexerState) -> None:
    r"""Advance ``state.i`` past every queued heredoc body, in delimiter order.

    Called once the newline that starts the heredoc body has already been
    consumed (``state.i`` sits at the first character of the body). Bash
    fulfills multiple heredocs opened on one logical line in the order their
    delimiters were declared, so the queue is drained front-to-back. Each body
    is read line-by-line from the RAW command text -- never re-tokenized -- so
    its content (arbitrary prose, code, even further ``<<``/``$(...)``-looking
    text) can never be mistaken for further shell syntax or fragment the
    command into bogus extra segments (the #1415/#1213 all-segments bug this
    fixes: a heredoc body's own newlines were previously emitted as command-
    separator tokens, splitting one logical redirect into many bogus
    "commands"). A line exactly equal to the delimiter (leading tabs stripped
    first when the operator was ``<<-``) terminates that heredoc; an
    unterminated heredoc (EOF reached with no matching line) stops at end of
    input rather than looping forever.
    """
    n = len(state.command)
    for delimiter, strip_tabs in state.pending_heredocs:
        while state.i <= n:
            line_end = state.command.find("\n", state.i)
            at_eof = line_end == -1
            end = n if at_eof else line_end
            line = state.command[state.i : end]
            candidate = line.lstrip("\t") if strip_tabs else line
            state.i = n if at_eof else end + 1
            if candidate == delimiter or at_eof:
                break
    state.pending_heredocs.clear()


def _consume_newline_operator(state: _LexerState) -> None:
    r"""Emit a single ``\n`` operator for a bare newline / ``\r\n``.

    ``raw`` carries the verbatim newline span (``"\n"`` or ``"\r\n"``) so the
    invariant "every token kind has a populated ``raw``" holds; the decoded
    ``value`` is normalised to ``"\n"`` regardless of the source line ending.

    A newline that follows a heredoc-open (``state.pending_heredocs`` non-empty)
    is the boundary that starts the heredoc body -- every queued body is
    consumed verbatim (:func:`_consume_pending_heredocs`) before the logical
    ``\n`` operator is emitted, so the heredoc-carrying segment is properly
    separated from whatever follows the terminator line without the body's
    own newlines ever acting as command separators.
    """
    state.flush()
    ch = state.command[state.i]
    consume = 2 if ch == "\r" and state.i + 1 < len(state.command) and state.command[state.i + 1] == "\n" else 1
    raw = state.command[state.i : state.i + consume]
    state.i += consume
    if state.pending_heredocs:
        _consume_pending_heredocs(state)
    state.tokens.append(Token("\n", TokenKind.OP, raw=raw))


def _match_operator(state: _LexerState) -> str | None:
    """Return the operator at the current cursor, longest-first."""
    for op in _TWO_CHAR_OPS:
        if state.command.startswith(op, state.i):
            return op
    ch = state.command[state.i]
    if ch in _ONE_CHAR_OPS:
        return ch
    return None


def _consume_ansi_c(state: _LexerState) -> None:
    r"""Consume a ``$'...'`` sequence at the current cursor."""
    state.begin_token()
    n = len(state.command)
    j = state.i + 2
    body_chars: list[str] = []
    while j < n:
        if state.command[j] == "\\" and j + 1 < n:
            body_chars.extend((state.command[j], state.command[j + 1]))
            j += 2
            continue
        if state.command[j] == "'":
            break
        body_chars.append(state.command[j])
        j += 1
    state.current.append(_decode_ansi_c("".join(body_chars)))
    state.i = j + 1


def _consume_single_quote(state: _LexerState) -> None:
    """Consume a ``'...'`` sequence — contents are verbatim."""
    state.begin_token()
    n = len(state.command)
    j = state.i + 1
    while j < n and state.command[j] != "'":
        state.current.append(state.command[j])
        j += 1
    state.i = j + 1


def _consume_double_quote(state: _LexerState) -> None:
    r"""Consume a ``"..."`` sequence — backslash escapes selected chars."""
    state.begin_token()
    n = len(state.command)
    j = state.i + 1
    while j < n and state.command[j] != '"':
        if state.command[j] == "\\" and j + 1 < n and state.command[j + 1] in _DOUBLE_QUOTE_ESCAPES:
            if state.command[j + 1] == "\n":
                # Line continuation inside double quotes — bash removes
                # the backslash-newline pair entirely.
                j += 2
                continue
            state.current.append(state.command[j + 1])
            j += 2
            continue
        state.current.append(state.command[j])
        j += 1
    state.i = j + 1


def _consume_unquoted_backslash(state: _LexerState) -> None:
    r"""Consume ``\X`` outside any quote — next char is literal."""
    state.begin_token()
    state.current.append(state.command[state.i + 1])
    state.i += 2


def _consume_comment(state: _LexerState) -> None:
    """Skip a ``#`` comment up to the next newline."""
    n = len(state.command)
    while state.i < n and state.command[state.i] not in _NEWLINE_CHARS:
        state.i += 1


def _consume_inline_whitespace(state: _LexerState) -> None:
    r"""Treat ``' '`` / ``\t`` as a token boundary."""
    state.flush()
    state.i += 1


def _try_consume_structured(state: _LexerState) -> bool:
    r"""Run the dispatch ladder for one character. Returns True iff consumed.

    Returning False means none of the structured handlers matched —
    :func:`tokenize` will then append the bare character to the current
    word token.
    """
    command = state.command
    n = len(command)
    ch = command[state.i]
    rule = _STRUCTURED_RULES.get(ch)
    if rule is not None:
        handler = rule(state, command, n)
        if handler is not None:
            handler(state)
            return True
    op = _match_operator(state)
    if op is not None:
        state.append_op(op, len(op))
        return True
    return False


# Lookup table mapping a leading char to a function that returns the
# right handler (or None when context disqualifies the dispatch).
_Handler = Callable[[_LexerState], None]
_Rule = Callable[[_LexerState, str, int], _Handler | None]


def _rule_inline_ws(_state: _LexerState, _command: str, _n: int) -> _Handler | None:
    return _consume_inline_whitespace


def _rule_backslash(state: _LexerState, command: str, n: int) -> _Handler | None:
    if state.i + 1 < n and command[state.i + 1] in _NEWLINE_CHARS:
        return _consume_line_continuation
    if state.i + 1 < n:
        return _consume_unquoted_backslash
    return None


def _rule_newline(_state: _LexerState, _command: str, _n: int) -> _Handler | None:
    return _consume_newline_operator


def _rule_dollar(state: _LexerState, command: str, n: int) -> _Handler | None:
    if state.i + 1 < n and command[state.i + 1] == "'":
        return _consume_ansi_c
    return None


def _rule_single_quote(_state: _LexerState, _command: str, _n: int) -> _Handler | None:
    return _consume_single_quote


def _rule_double_quote(_state: _LexerState, _command: str, _n: int) -> _Handler | None:
    return _consume_double_quote


def _rule_hash(state: _LexerState, _command: str, _n: int) -> _Handler | None:
    if state.in_token:
        return None
    return _consume_comment


_STRUCTURED_RULES: Final[dict[str, _Rule]] = {
    " ": _rule_inline_ws,
    "\t": _rule_inline_ws,
    "\\": _rule_backslash,
    "\n": _rule_newline,
    "\r": _rule_newline,
    "$": _rule_dollar,
    "'": _rule_single_quote,
    '"': _rule_double_quote,
    "#": _rule_hash,
}


def tokenize(command: str) -> list[Token]:
    r"""Return the bash-equivalent token stream for ``command``.

    The result preserves operator tokens (``;`` / ``|`` / ``&`` /
    ``&&`` / ``||`` / ``\n``) as standalone :class:`Token` instances
    with :attr:`TokenKind.OP`, and emits regular words as
    :attr:`TokenKind.WORD` with their decoded value. Line continuations
    inside a quoted region are preserved literally; outside any quote
    they are eliminated when token-internal and treated as whitespace
    when between tokens.
    """
    state = _LexerState(command=command, tokens=[], current=[], in_token=False, i=0)
    n = len(command)
    while state.i < n:
        if _try_consume_structured(state):
            continue
        state.begin_token()
        state.current.append(command[state.i])
        state.i += 1
    state.flush()
    return state.tokens


# Operator values that delimit one logical command from the next.
_COMMAND_SEPARATORS: Final[frozenset[str]] = frozenset({";", "|", "&", "&&", "||", "\n"})


def is_command_separator(token: Token) -> bool:
    """Return True iff ``token`` ends the current command segment."""
    return token.kind is TokenKind.OP and token.value in _COMMAND_SEPARATORS


def split_commands(tokens: list[Token]) -> list[list[Token]]:
    """Group a token stream into per-command segments.

    Each segment is the list of WORD tokens between command-separator
    operators. Empty segments (back-to-back separators) are dropped.
    """
    segments: list[list[Token]] = []
    current: list[Token] = []
    for tok in tokens:
        if is_command_separator(tok):
            if current:
                segments.append(current)
                current = []
        else:
            current.append(tok)
    if current:
        segments.append(current)
    return segments
