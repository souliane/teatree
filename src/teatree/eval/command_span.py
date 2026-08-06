"""The EXECUTED span of a shell command — the program, with literal payloads elided.

A negative eval matcher grades an ACT. A quoted argument is prose the model authored,
so a forbidden phrase inside it is a REPORT of the act, not the act — the same bytes
are correct evidence in one span and a violation in the other. Eliding the literal
payloads leaves what the shell would actually run.

Elision is opt-in per matcher (``Bash.command_span``); plain ``Bash.command`` still
grades the whole string, because for a large class the payload IS the graded artifact.
"""

import re

#: The token before a quoted operand that makes that operand a SCRIPT rather than
#: payload — ``bash -c '…'``, ``eval "…"``. A script is executed, so it stays whole.
_SCRIPT_OPERAND_TOKENS = frozenset({"-c", "eval"})

_TOKEN_BOUNDARY = re.compile(r"[\s;|&()]")

_HEREDOC_OP = re.compile(
    r"""<<-?[ \t]*(?:
        (?P<quote>['"])(?P<quoted_delim>[^'"\n]+)(?P=quote)
        | (?P<delim>[A-Za-z_][A-Za-z0-9_]*)
    )""",
    re.VERBOSE,
)


def executed_span(command: str) -> str:
    """*command* with its literal quoted payloads elided.

    Single-quoted regions and double-quoted regions are dropped; ``$( … )`` and
    backtick bodies inside double quotes survive (a substitution is executed), as
    does the quoted operand of ``-c``/``eval``. A quoted-delimiter heredoc body is
    dropped; an unquoted-delimiter one is kept. Anything unparsable — an unbalanced
    quote, a heredoc with no terminator — keeps the remainder RAW, so a stray
    apostrophe can never silently strip a matcher's teeth.
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
            elif self._src.startswith("<<", self._pos) and not self._src.startswith("<<<", self._pos):
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

    def _scan_single_quoted(self) -> None:
        close = self._src.find("'", self._pos + 1)
        if close == -1:
            self._keep_raw_remainder()
            return
        if self._preceding_token() in _SCRIPT_OPERAND_TOKENS:
            self._emit(self._src[self._pos : close + 1])
        self._pos = close + 1

    def _scan_double_quoted(self) -> None:
        close = _closing_double_quote(self._src, self._pos)
        if close is None:
            self._keep_raw_remainder()
            return
        if self._preceding_token() in _SCRIPT_OPERAND_TOKENS:
            self._emit(self._src[self._pos : close + 1])
        else:
            self._emit(_substitutions(self._src[self._pos + 1 : close]))
        self._pos = close + 1

    def _scan_heredoc_operator(self) -> None:
        match = _HEREDOC_OP.match(self._src, self._pos)
        if match is None:
            self._emit(self._src[self._pos : self._pos + 2])
            self._pos += 2
            return
        quoted = match["quoted_delim"]
        self._emit(match.group(0))
        self._pending_heredocs.append((quoted or match["delim"], quoted is not None))
        self._pos = match.end()

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
    depth = 0
    for index in range(open_index, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index
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
