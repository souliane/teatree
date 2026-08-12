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
from collections.abc import Iterator

#: The token before a quoted operand that makes that operand a SCRIPT rather than
#: payload — ``bash -c '…'``, ``eval "…"``, and the clustered short-option runs
#: (``-lc``, ``-ec``) carrying the same operand. A script is executed, so it stays whole.
_SCRIPT_OPERAND_TOKEN = re.compile(r"eval|-[A-Za-z]*c")

#: Programs that consume a redirected heredoc body or here-string operand as DATA and
#: never execute it. The decision is stated this way round because the complement — an
#: allowlist of INTERPRETER names — has an unbounded tail: ``. /dev/stdin``, ``source
#: /dev/stdin`` and ``while read -r l; do eval "$l"; done`` all execute the redirected
#: text while naming no interpreter, so enumerating interpreters cannot terminate.
#: Widen this set only by name, with the case that forced it.
_STDIN_READERS = frozenset({"cat", "egrep", "fgrep", "grep", "head", "sort", "t3", "tail", "tee", "tr", "uniq", "wc"})

#: Words at or above which a standalone quoted operand reads as prose rather than as a
#: fragment of the command's own word chain (``t3 example 'ticket clear' 42``). Below it
#: the two are indistinguishable, and eliding an act is the failure that costs teeth.
_PROSE_WORD_FLOOR = 4

_TOKEN_BOUNDARY = re.compile(r"[\s;|&()]")

#: ``NAME=value`` prefixing a stage's command word — an assignment, not the program.
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)

#: Words a compound command opens with that precede its command word without being it.
_NOT_A_COMMAND = frozenset({"(", "{", "!", "if", "then", "elif", "else", "while", "until", "do", "time"})

#: Characters that end the command segment the cursor sits in. A single ``|`` is absent
#: on purpose: a pipeline is ONE segment, because ``cat <<'EOF' | bash`` executes its body.
_SEGMENT_END = ";&\n)}"

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
    quoted operand of ``-c``/``eval``. Redirected text — a quoted-delimiter heredoc
    body, a ``<<<`` here-string operand — is dropped only where every stage of the
    command segment provably just READS its stdin (:data:`_STDIN_READERS`); an unknown,
    expanded, globbed or compound command word leaves it kept, however long it is.
    An unquoted-delimiter heredoc body is always kept. A kept region has its quotes
    removed, as the shell removes them. Anything unparsable — an unbalanced quote, a
    heredoc with no terminator — keeps the remainder verbatim, so neither a stray
    apostrophe nor a quoted fragment of the act can silently strip a matcher's teeth.
    """
    return _SpanScanner(command).run()


class _SpanScanner:
    def __init__(self, command: str) -> None:
        self._src = command
        self._out: list[str] = []
        self._pos = 0
        self._pending_heredocs: list[tuple[str, bool]] = []
        self._segment_start = 0
        self._herestring_pending = False
        self._herestring_word = ""

    def run(self) -> str:
        while self._pos < len(self._src):
            char = self._src[self._pos]
            self._track_herestring_operand(char)
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
                self._segment_start = self._pos
            elif self._src.startswith(_HERESTRING_OP, self._pos):
                self._emit(_HERESTRING_OP)
                self._pos += len(_HERESTRING_OP)
                self._herestring_pending = True
            elif self._src.startswith("<<", self._pos):
                self._scan_heredoc_operator()
            else:
                self._emit(char)
                self._pos += 1
                if char in _SEGMENT_END or char == "|":
                    self._segment_start = self._pos
        return "".join(self._out)

    def _track_herestring_operand(self, char: str) -> None:
        """Follow the ``<<<`` operand word so every quoted region of it gets one verdict.

        ``<<<'t3 widget task '"complete 42"`` is ONE word bash hands to the command;
        re-deriving the verdict from the text before each region gives the second half
        the attached-payload rule and drops half an executed act, and counting the prose
        floor per region reassembles a payload out of two sub-floor halves.
        """
        if self._herestring_pending:
            if not char.isspace():
                self._herestring_pending = False
                self._herestring_word = self._src[self._pos : _word_end(self._src, self._pos)]
        elif self._herestring_word and (char.isspace() or char in ";|&()<>"):
            self._herestring_word = ""

    def _emit(self, text: str) -> None:
        self._out.append(text)

    def _keep_raw_remainder(self) -> None:
        self._emit(self._src[self._pos :])
        self._pos = len(self._src)

    def _preceding_token(self) -> str | None:
        r"""The word before the cursor as bash resolves it, or ``None`` if only bash can.

        Spliced, then unquoted and unescaped, so ``bash -c \``+newline+``'…'``,
        ``'eval' '…'`` and ``\\eval '…'`` reach the script-operand rule that the raw
        spelling misses. ``$x`` resolves at runtime, so it is undecidable here.
        """
        word = _TOKEN_BOUNDARY.split(_splice(self._src[: self._pos]).rstrip())[-1]
        return _resolve_word(word) if word else ""

    def _keeps(self, body: str) -> bool:
        """Whether the quoted region at the cursor survives into the span."""
        if self._herestring_word:
            # The operand of ``<<<`` is redirected text, decided by the same rule as a
            # heredoc body. Reaching the attached-payload rule below instead would read
            # the operator's own ``<`` as the unquoted word fragment of ``-m'…'`` and
            # drop the act — the silent failure this module exists to prevent.
            return not self._is_data_only() or len(self._herestring_word.split()) < _PROSE_WORD_FLOOR
        token = self._preceding_token()
        if token is None or _SCRIPT_OPERAND_TOKEN.fullmatch(token) is not None:
            return True
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
        elide = quoted is not None and self._is_data_only()
        self._pending_heredocs.append((quoted or match["delim"], elide))
        self._pos = match.end()

    def _is_data_only(self) -> bool:
        r"""Whether every stage of the segment at the cursor merely READS its stdin.

        Serves both the ``<<`` heredoc operator and the ``<<<`` here-string operand, and
        answers on POSITIVE proof only: an unknown, expanded, globbed or compound command
        word leaves the segment unproven, and unproven keeps. The window is the command
        segment the scanner is inside — not the physical line — so a ``\``+newline
        continuation, a newline inside a quoted argument and an intervening heredoc body
        all stop bounding the words bash resolves into one command.
        """
        segment = self._src[self._segment_start : _segment_end(self._src, self._segment_start)]
        return all(_stage_command_word(stage) in _STDIN_READERS for stage in _pipeline_stages(segment))

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


def _splice(text: str) -> str:
    """*text* with its line continuations joined, as bash joins them before parsing."""
    return text.replace("\\\n", "")


def _resolve_word(raw: str) -> str | None:
    r"""*raw* with quotes and escapes removed, or ``None`` when only bash can resolve it.

    ``'bash'``, ``b"as"h`` and ``\bash`` are all the word ``bash``; ``$SHELL``,
    ``$(which bash)`` and ``/bin/b?sh`` name a program at runtime, not here. ``None`` is
    the fail-closed answer, so an unresolvable word is never proved to be a reader.
    """
    out: list[str] = []
    index = 0
    while index < len(raw):
        char = raw[index]
        if char == "\\":
            if index + 1 >= len(raw):
                return None
            out.append(raw[index + 1])
            index += 2
        elif char == "'":
            close = raw.find("'", index + 1)
            if close == -1:
                return None
            out.append(raw[index + 1 : close])
            index = close + 1
        elif char == '"':
            close = _closing_double_quote(raw, index)
            if close is None:
                return None
            body = raw[index + 1 : close]
            if "$" in body or "`" in body:
                return None
            out.append(body.replace("\\", ""))
            index = close + 1
        elif char in "$`*?[":
            return None
        else:
            out.append(char)
            index += 1
    return "".join(out) or None


def _unquoted_scan(text: str, start: int = 0) -> Iterator[tuple[int, str]]:
    """``(index, char)`` for each char of *text* outside a quote, escape or substitution."""
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
        elif char == "'":
            close = text.find("'", index + 1)
            index = len(text) if close == -1 else close + 1
        elif char == '"':
            close = _closing_double_quote(text, index)
            index = len(text) if close is None else close + 1
        elif char == "`":
            close = text.find("`", index + 1)
            index = len(text) if close == -1 else close + 1
        elif text.startswith("$(", index):
            close = _matching_paren(text, index + 1)
            index = len(text) if close is None else close + 1
        elif text.startswith("${", index):
            close = text.find("}", index + 2)
            index = len(text) if close == -1 else close + 1
        else:
            yield index, char
            index += 1


def _word_end(text: str, start: int) -> int:
    """Index just past the single shell word beginning at *start*."""
    for index, char in _unquoted_scan(text, start):
        if char.isspace() or char in ";|&()<>":
            return index
    return len(text)


def _segment_end(text: str, start: int) -> int:
    """Index just past the command segment starting at *start* — its whole pipeline."""
    for index, char in _unquoted_scan(text, start):
        if char in _SEGMENT_END or (char == "|" and text.startswith("||", index)):
            return index
    return len(text)


def _pipeline_stages(segment: str) -> list[str]:
    """*segment* cut at each unquoted ``|`` — one entry per program bash would run."""
    stages: list[str] = []
    cut = 0
    for index, char in _unquoted_scan(segment):
        if char == "|":
            stages.append(segment[cut:index])
            cut = index + 1
    return [*stages, segment[cut:]]


def _quoted_region_end(text: str, start: int) -> int | None:
    """Index just past the quoted region opening at *start*, or ``None`` if it is unclosed."""
    if text[start] == "'":
        close = text.find("'", start + 1)
        return None if close == -1 else close + 1
    close = _closing_double_quote(text, start)
    return None if close is None else close + 1


def _stage_tokens(stage: str) -> Iterator[str]:
    """*stage* cut into words and bare redirection characters, continuations spliced away.

    A redirection operator needs no space in front of it, so ``bash<<<'…'`` is one
    whitespace-delimited token whose command word a whitespace-only split never sees.
    """
    word: list[str] = []
    index = 0
    while index < len(stage):
        char = stage[index]
        if char == "\\" and index + 1 < len(stage):
            word += [stage[index : index + 2]] if stage[index + 1] != "\n" else []
            index += 2
        elif char in "'\"":
            close = _quoted_region_end(stage, index)
            if close is None:
                break
            word.append(stage[index:close])
            index = close
        elif char.isspace() or char in "<>":
            yield from ["".join(word)] if word else []
            word = []
            yield from [char] if char in "<>" else []
            index += 1
        else:
            word.append(char)
            index += 1
    yield from ["".join(word)] if word else []


def _stage_words(stage: str) -> Iterator[str]:
    """*stage*'s command words — its redirections, their targets and fd numbers removed."""
    pending: str | None = None
    skip_target = False
    for token in _stage_tokens(stage):
        if token in {"<", ">"}:
            pending = None if pending is not None and pending.isdigit() else pending
            skip_target = True
        elif skip_target:
            skip_target = False
        else:
            yield from [pending] if pending is not None else []
            pending = token
    yield from [pending] if pending is not None else []


def _stage_command_word(stage: str) -> str | None:
    """The basename of the program *stage* runs, or ``None`` when it is unresolvable."""
    for word in _stage_words(stage):
        if word in _NOT_A_COMMAND or _ASSIGNMENT.fullmatch(word) is not None:
            continue
        resolved = _resolve_word(word)
        return None if resolved is None else resolved.rsplit("/", 1)[-1]
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
