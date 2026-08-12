"""The EXECUTED span of a shell command — the program, with literal payloads elided.

A negative eval matcher grades an ACT. A quoted payload is prose the model authored,
so a forbidden phrase inside it is a REPORT of the act, not the act — the same bytes
are correct evidence in one span and a violation in the other. Eliding the literal
payloads leaves what the shell would actually run.

The elision is asymmetric on purpose: dropping text the shell executes strips a
matcher's teeth SILENTLY, while keeping a payload only ever reds loudly. So a region
the scanner cannot name a payload is kept.

Elision is opt-in per matcher (``Bash.command_span``); plain ``Bash.command`` still
grades the whole string, because for a large class the payload IS the graded artifact.
"""

import re

#: The token before a quoted operand that makes that operand a SCRIPT rather than
#: payload — ``bash -c '…'``, ``eval "…"``, and the clustered short-option runs
#: (``-lc``, ``-ec``) carrying the same operand. A script is executed, so it stays whole.
_SCRIPT_OPERAND_TOKEN = re.compile(r"eval|-[A-Za-z]*c")

#: A heredoc or here-string redirected into one of these is a script on stdin — a
#: quoted delimiter suppresses expansion, it does not stop the body being executed.
_SCRIPT_INTERPRETERS = frozenset({"bash", "sh", "dash", "ksh", "zsh", "python", "python3", "node", "perl", "ruby"})

#: Words at or above which a standalone quoted operand reads as prose rather than as a
#: fragment of the command's own word chain (``t3 example 'ticket clear' 42``). Below it
#: the two are indistinguishable, and eliding an act is the failure that costs teeth.
_PROSE_WORD_FLOOR = 4

_TOKEN_BOUNDARY = re.compile(r"[\s;|&()]")

#: The boundary the INTERPRETER lookup splits on. A redirection operator needs no space
#: in front of it, so ``bash<<<'…'`` and ``bash<<'EOF'`` are one ``_TOKEN_BOUNDARY``
#: token and the interpreter never matches — the branch falls through to the prose floor
#: and drops an act the shell genuinely runs. Redirection characters therefore bound a
#: word HERE, though not in :meth:`_SpanScanner._preceding_token`, where the operator's
#: own ``<`` must stay attached for the here-string branch to recognise it.
_INTERPRETER_BOUNDARY = re.compile(r"[\s;|&()<>]")

#: The here-string operator. Its operand is a whole word the shell hands to the command
#: on stdin, so the ``<`` in front of it is an OPERATOR, never the unquoted word fragment
#: that marks attached payload (``-m'…'``).
_HERESTRING_OP = "<<<"

_HEREDOC_OP = re.compile(
    r"""<<-?[ \t]*(?:
        (?P<quote>['"])(?P<quoted_delim>[^'"\n]+)(?P=quote)
        | (?P<delim>[A-Za-z_][A-Za-z0-9_]*)
    )""",
    re.VERBOSE,
)


def executed_span(command: str) -> str:
    """*command* with its literal quoted payloads elided.

    A quoted region is dropped only where the scanner can NAME it a payload: attached
    to an unquoted word fragment (``-m'…'``, ``--body='…'``), or a standalone operand
    of :data:`_PROSE_WORD_FLOOR` words or more. ``$( … )`` and backtick bodies inside a
    dropped double-quoted region survive (a substitution is executed), as does the
    quoted operand of ``-c``/``eval``. A quoted-delimiter heredoc body is dropped
    unless its line runs an interpreter, which executes it; an unquoted-delimiter one
    is kept. A ``<<<`` here-string operand is read the same way — an interpreter runs
    it as a script on stdin, so it is kept whole however long it is, quoted or not.
    A kept region has its quotes removed, as the shell removes them. Anything
    unparsable — an unbalanced quote, a heredoc with no terminator — keeps the
    remainder verbatim, so neither a stray apostrophe nor a quoted fragment of the act
    can silently strip a matcher's teeth.
    """
    return _SpanScanner(command).run()


class _SpanScanner:
    def __init__(self, command: str) -> None:
        self._src = command
        self._out: list[str] = []
        self._pos = 0
        self._pending_heredocs: list[tuple[str, bool]] = []

    def run(self) -> str:
        while self._pos < len(self._src):
            char = self._src[self._pos]
            if char == "\\" and self._pos + 1 < len(self._src):
                self._emit(self._src[self._pos : self._pos + 2])
                self._pos += 2
            elif char == "'":
                self._scan_single_quoted()
            elif char == '"':
                self._scan_double_quoted()
            elif char == "\n":
                self._emit(char)
                self._pos += 1
                self._drain_heredoc_bodies()
            elif self._src.startswith(_HERESTRING_OP, self._pos):
                self._emit(_HERESTRING_OP)
                self._pos += len(_HERESTRING_OP)
            elif self._src.startswith("<<", self._pos):
                self._scan_heredoc_operator()
            else:
                self._emit(char)
                self._pos += 1
        return "".join(self._out)

    def _emit(self, text: str) -> None:
        self._out.append(text)

    def _keep_raw_remainder(self) -> None:
        self._emit(self._src[self._pos :])
        self._pos = len(self._src)

    def _preceding_token(self) -> str:
        head = self._src[: self._pos].rstrip()
        return _TOKEN_BOUNDARY.split(head)[-1] if head else ""

    def _keeps(self, body: str) -> bool:
        """Whether the quoted region at the cursor survives into the span."""
        token = self._preceding_token()
        if _SCRIPT_OPERAND_TOKEN.fullmatch(token) is not None:
            return True
        if token.endswith(_HERESTRING_OP):
            # An interpreter RUNS its here-string operand, exactly as it runs a
            # quoted-delimiter heredoc body, so the operand is a script and stays
            # whole. Reaching the attached-payload rule below instead would read
            # the operator's own ``<`` as the unquoted word fragment of ``-m'…'``
            # and drop the act — the silent failure this module exists to prevent.
            return self._feeds_an_interpreter() or len(body.split()) < _PROSE_WORD_FLOOR
        if self._pos > 0 and _TOKEN_BOUNDARY.match(self._src[self._pos - 1]) is None:
            return False
        return len(body.split()) < _PROSE_WORD_FLOOR

    def _scan_single_quoted(self) -> None:
        close = self._src.find("'", self._pos + 1)
        if close == -1:
            self._keep_raw_remainder()
            return
        body = self._src[self._pos + 1 : close]
        if self._keeps(body):
            self._emit(body)
        self._pos = close + 1

    def _scan_double_quoted(self) -> None:
        close = _closing_double_quote(self._src, self._pos)
        if close is None:
            self._keep_raw_remainder()
            return
        body = self._src[self._pos + 1 : close]
        self._emit(body if self._keeps(body) else _substitutions(body))
        self._pos = close + 1

    def _scan_heredoc_operator(self) -> None:
        match = _HEREDOC_OP.match(self._src, self._pos)
        if match is None:
            self._emit(self._src[self._pos : self._pos + 2])
            self._pos += 2
            return
        quoted = match["quoted_delim"]
        self._emit(match.group(0))
        elide = quoted is not None and not self._feeds_an_interpreter()
        self._pending_heredocs.append((quoted or match["delim"], elide))
        self._pos = match.end()

    def _feeds_an_interpreter(self) -> bool:
        """Whether the line at the cursor runs its redirected body as a script.

        Serves both the ``<<`` heredoc operator and the ``<<<`` here-string operand.
        Splitting on :data:`_INTERPRETER_BOUNDARY` sees an interpreter abutting its own
        redirection operator (``bash<<<'…'``), which a whitespace-only split reads as
        one token and misses.
        """
        line_start = self._src.rfind("\n", 0, self._pos) + 1
        line_end = self._src.find("\n", self._pos)
        line = self._src[line_start:] if line_end == -1 else self._src[line_start:line_end]
        return any(word.rsplit("/", 1)[-1] in _SCRIPT_INTERPRETERS for word in _INTERPRETER_BOUNDARY.split(line))

    def _drain_heredoc_bodies(self) -> None:
        while self._pending_heredocs:
            delimiter, elide = self._pending_heredocs.pop(0)
            body_end = self._body_end(delimiter)
            if body_end is None:
                self._keep_raw_remainder()
                return
            if not elide:
                self._emit(self._src[self._pos : body_end])
            self._pos = body_end

    def _body_end(self, delimiter: str) -> int | None:
        """Index just past the terminator line closing a heredoc body, or ``None``."""
        cursor = self._pos
        while cursor < len(self._src):
            newline = self._src.find("\n", cursor)
            line_end = len(self._src) if newline == -1 else newline + 1
            if self._src[cursor:line_end].strip() == delimiter:
                return line_end
            cursor = line_end
        return None


def _closing_double_quote(text: str, start: int) -> int | None:
    """Index of the ``"`` closing the region opened at *start*, or ``None``."""
    index = start + 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
        elif char == '"':
            return index
        elif text.startswith("$(", index):
            end = _matching_paren(text, index + 1)
            if end is None:
                return None
            index = end + 1
        elif char == "`":
            backtick = text.find("`", index + 1)
            if backtick == -1:
                return None
            index = backtick + 1
        else:
            index += 1
    return None


def _matching_paren(text: str, open_index: int) -> int | None:
    """Index of the ``)`` closing the ``(`` at *open_index*, or ``None``.

    Quoted regions are skipped whole — a ``)`` inside one closes nothing, so a
    substitution is never truncated mid-way.
    """
    depth = 0
    index = open_index
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
        elif char == "'":
            close = text.find("'", index + 1)
            if close == -1:
                return None
            index = close + 1
        elif char == '"':
            close = _closing_double_quote(text, index)
            if close is None:
                return None
            index = close + 1
        elif char == "(":
            depth += 1
            index += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
            index += 1
        else:
            index += 1
    return None


def _substitutions(body: str) -> str:
    """The ``$( … )`` and backtick spans of a double-quoted *body*, joined."""
    spans: list[str] = []
    index = 0
    while index < len(body):
        if body[index] == "\\":
            index += 2
        elif body.startswith("$(", index):
            end = _matching_paren(body, index + 1)
            if end is None:
                break
            spans.append(body[index : end + 1])
            index = end + 1
        elif body[index] == "`":
            backtick = body.find("`", index + 1)
            if backtick == -1:
                break
            spans.append(body[index : backtick + 1])
            index = backtick + 1
        else:
            index += 1
    return " ".join(spans)
