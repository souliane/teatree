r"""Where bash's grammar puts a BOUNDARY — the quote-aware scan and the reachability window.

:mod:`teatree.eval.command_span` decides which regions of a command are payload. That
verdict rests on two questions this module answers, both purely about bash's grammar: which
characters are outside a quoted region, and how far the text redirected at one point can
travel before a control operator ends the command list. What a word RESOLVES to, and which
regions are payload, stay with the scanner.

The window is the load-bearing half. Redirected text is elided only where every program it
can REACH provably just reads its stdin, so a window that ends too early is left holding a
lone reader, proves data-only, and drops an act SILENTLY. Every rule here therefore errs
towards ending the window LATER: see :class:`Nesting` for why that direction is safe.
"""

import re
from collections.abc import Iterator

#: Characters bash reads as a control operator ENDING a command list. A single ``|`` is
#: absent on purpose: a pipeline is ONE segment, because ``cat <<'EOF' | bash`` executes
#: its body. ``&`` is here conditionally — see :func:`ends_a_list`.
LIST_TERMINATOR = ";&\n"

#: Openers and closers of a command GROUP. A terminator inside one ends nothing at the
#: outer level, and a group's stdout is its contents' stdout, so the window has to widen
#: through both — ``{ cat <<<'…'; } | bash`` feeds the here-string to ``bash``.
GROUP_OPEN = "({"
GROUP_CLOSE = ")}"

#: The other compounds whose stdout is their body's stdout, each mapped to the word closing
#: it. Their INTERNAL ``;`` or newline is no list terminator at the enclosing level, so a
#: window that reads one as such truncates before the ``| bash`` the body's stdout reaches
#: and is left holding a lone reader — ``if true; then cat <<<'…'; fi | bash``.
_COMPOUND_CLOSER = {"case": "esac", "for": "done", "if": "fi", "select": "done", "until": "done", "while": "done"}

#: Words after which a compound keyword is still the next command word (``then if …``).
#: Granted to OPENERS only: a false push merely widens the window, while a closer popped
#: after ``for i in done`` would narrow it — the one direction that can newly drop an act.
_COMMAND_POSITION_WORDS = frozenset({"do", "elif", "else", "in", "then"})

_COMMAND_POSITION_CHARS = "\n&(){};|"

_WORD_START_CHARS = "\t\n &(){};<>|"

#: A function DEFINITION header immediately before a group opener. A body's stdout flows to
#: the CALL site, textually elsewhere (``f() { cat <<<'…'; }; f | bash``), so no window over
#: the definition can see that pipe and a redirection inside one is never proved data-only.
_FUNCTION_HEADER = re.compile(
    r"(?:^|[\s;&|(){}])(?:(?:function\s+)?[\w.-]+\s*\(\s*\)|function\s+[\w.-]+)\s*$",
)


def unquoted_scan(text: str, start: int = 0) -> Iterator[tuple[int, str]]:
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
            close = closing_double_quote(text, index)
            index = len(text) if close is None else close + 1
        elif char == "`":
            close = text.find("`", index + 1)
            index = len(text) if close == -1 else close + 1
        elif text.startswith("$(", index):
            close = matching_paren(text, index + 1)
            index = len(text) if close is None else close + 1
        elif text.startswith("${", index):
            close = text.find("}", index + 2)
            index = len(text) if close == -1 else close + 1
        else:
            yield index, char
            index += 1


def word_end(text: str, start: int) -> int:
    """Index just past the single shell word beginning at *start*."""
    for index, char in unquoted_scan(text, start):
        if char.isspace() or char in ";|&()<>":
            return index
    return len(text)


def closing_double_quote(text: str, start: int) -> int | None:
    """Index of the ``"`` closing the region opened at *start*, or ``None``."""
    index = start + 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
        elif char == '"':
            return index
        elif text.startswith("$(", index):
            end = matching_paren(text, index + 1)
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


def matching_paren(text: str, open_index: int) -> int | None:
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
            close = closing_double_quote(text, index)
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


def quoted_region_end(text: str, start: int) -> int | None:
    """Index just past the quoted region opening at *start*, or ``None`` if it is unclosed."""
    if text[start] == "'":
        close = text.find("'", start + 1)
        return None if close == -1 else close + 1
    close = closing_double_quote(text, start)
    return None if close is None else close + 1


def ends_a_list(text: str, index: int) -> bool:
    r"""Whether *text*\ [*index*] is a control operator that ends a command list.

    A raw ``&`` is one only OUTSIDE a redirection operator: ``2>&1``, ``2>&-``, ``&>``
    and ``|&`` all spell it inside one, where bash reads no list boundary at all. Taking
    the character at face value cuts the window short of a later ``| bash``, which then
    reads as a segment every stage of which is a reader — the silent elision.
    """
    char = text[index]
    if char not in LIST_TERMINATOR:
        return False
    if char != "&":
        return True
    if text[index + 1 : index + 2] == ">":
        return False
    before = index - 1
    while before >= 0 and text[before] in " \t":
        before -= 1
    return before < 0 or text[before] not in "><|"


class Nesting:
    r"""The compound commands a left-to-right scan is inside — groups AND keyword compounds.

    Every rule here only ever DELAYS the window's end, which is what makes the widening
    unable to introduce a silent drop: the data-only proof is ``all(stage is a reader)``, so
    a longer window either appends to the LAST stage — whose command word is its first word,
    hence unchanged — or adds whole stages, each an extra conjunct. It can turn the proof
    True→False, never False→True.

    Keyword compounds are stacked SEPARATELY from the ``(``/``{`` counter for that reason: a
    stray ``fi`` cancelling a real brace is the one move that could end the window earlier
    than a group-only scan. So openers are recognised liberally and closers only in command
    position against a matching top of stack.
    """

    def __init__(self) -> None:
        #: One entry per open group, true where a function-definition header opened it.
        self.groups: list[bool] = []
        #: ``(keyword, group depth at the push)`` per open keyword compound.
        self.keywords: list[tuple[str, int]] = []

    def in_a_function_body(self) -> bool:
        return any(self.groups)

    def closed(self) -> bool:
        """Whether a control operator at the cursor ends the enclosing command list."""
        return not self.groups and not self.keywords

    def feed(self, text: str, index: int) -> None:
        char = text[index]
        if char in GROUP_OPEN:
            self.groups.append(_FUNCTION_HEADER.search(text[:index]) is not None)
        elif char in GROUP_CLOSE:
            if self.groups and not self._closes_a_case_pattern(char):
                self.groups.pop()
        elif char.isalpha() and (index == 0 or text[index - 1] in _WORD_START_CHARS):
            self._feed_word(text, index)

    def _closes_a_case_pattern(self, char: str) -> bool:
        """Whether ``)`` here ends a ``case`` PATTERN, which closes no group.

        ``{ case x in x) …;; esac; } | bash`` would otherwise have its brace depth
        decremented by a pattern, narrowing the window short of the pipe.
        """
        return char == ")" and bool(self.keywords) and self.keywords[-1] == ("case", len(self.groups))

    def _feed_word(self, text: str, index: int) -> None:
        word = text[index : word_end(text, index)]
        if word in _COMPOUND_CLOSER and _in_command_position(text, index, after_a_word=True):
            self.keywords.append((word, len(self.groups)))
        elif (
            self.keywords
            and word == _COMPOUND_CLOSER[self.keywords[-1][0]]
            and _in_command_position(text, index, after_a_word=False)
        ):
            self.keywords.pop()


def _in_command_position(text: str, index: int, *, after_a_word: bool) -> bool:
    """Whether the word at *index* sits where bash reads a command name.

    *after_a_word* additionally admits ``then``/``do``/``in`` and friends, which precede a
    command word without being one. Without the check ``echo if`` would push a compound.
    """
    cursor = index - 1
    while cursor >= 0 and text[cursor] in " \t":
        cursor -= 1
    if cursor < 0:
        return True
    if text[cursor] in _COMMAND_POSITION_CHARS:
        return True
    if not after_a_word:
        return False
    end = cursor + 1
    while cursor >= 0 and text[cursor] not in _WORD_START_CHARS:
        cursor -= 1
    return text[cursor + 1 : end] in _COMMAND_POSITION_WORDS


def enclosing(text: str, end: int) -> Nesting:
    """The compound nesting still open just before *end*."""
    nesting = Nesting()
    for index, _ in unquoted_scan(text[:end]):
        nesting.feed(text, index)
    return nesting


def segment_end(text: str, start: int, nesting: Nesting) -> int:
    """Index just past every program the text redirected at *start* can reach.

    *nesting* is what encloses *start*, so the scan runs past those compounds' own
    terminators and closers to the outer pipeline: their stdout is their contents' stdout,
    which is what carries the redirected text out of ``{ … }`` or ``if … fi`` into ``| bash``.
    An unterminated compound leaves the nesting open, so the window runs to end of text.
    """
    for index, char in unquoted_scan(text, start):
        nesting.feed(text, index)
        if char in GROUP_OPEN or char in GROUP_CLOSE:
            continue
        if nesting.closed() and (ends_a_list(text, index) or text.startswith("||", index)):
            return index
    return len(text)
