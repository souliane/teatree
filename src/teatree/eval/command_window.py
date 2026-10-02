r"""Where bash's grammar puts a BOUNDARY — the quote-aware scan and the reachability window.

:mod:`teatree.eval.command_span` decides which regions of a command are payload. That
verdict rests on two questions this module answers, both purely about bash's grammar: which
characters are outside a quoted region, and how far the text redirected at one point can
travel before a control operator ends the command list. What a word RESOLVES to, and which
regions are payload, stay with the scanner.

The window is the load-bearing half. Redirected text is elided only where every program it
can REACH provably just reads its stdin, so a window that ends too early is left holding a
lone reader, proves data-only, and drops an act SILENTLY. Seven passes of this module each
taught it one more construct and each shipped a fresh construct it did not know, because an
ENUMERATION cannot say what it is missing.

So the guarantee is structural rather than enumerated: :class:`Recogniser` walks the text
against an explicit accept table, and :func:`segment_end` bounds the window only where every
token from the segment start to that bound was positively recognised. Anything else — an
arithmetic compound, a double-bracket test, an array assignment, a closer matching no open
frame — leaves the window running to end of text. An unmodelled shape therefore OVER-keeps,
which reds a matcher loudly, instead of ELIDING, which strips its teeth in silence. The
default is the invariant; :data:`RECOGNISED_COMPOUNDS` is the tunable, retired one construct
at a time by ADDING recognition, never by loosening the default.
"""

from collections.abc import Iterator
from dataclasses import dataclass

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
#: and is left holding a lone reader — ``if true; then cat <<<'…'; fi | bash``. ``select``
#: is deliberately absent: it is a compound, but an unrecognised one only ever widens.
_COMPOUND_CLOSER = {"case": "esac", "for": "done", "if": "fi", "until": "done", "while": "done"}

#: Every production :class:`Recogniser` accepts as a compound command. The generative sweep
#: in ``tests/eval_replay/test_command_span.py`` emits exactly this set, so teaching the
#: recogniser a construct without teaching the generator to produce it reds.
RECOGNISED_COMPOUNDS = frozenset({"brace-group", "subshell", "function-definition", *_COMPOUND_CLOSER})

#: The words closing a compound, plus the brace group's. Recognised only in command
#: position and only against a MATCHING open frame; anything else fails closed.
_CLOSER_WORDS = frozenset({"esac", "done", "fi", "}"})

#: Compounds whose head is itself a command list, so the word after them is a command word.
#: ``for``/``case`` are followed by a name and a word list instead, which is why a ``done``
#: spelt as one of their words (``for i in done``) is never read as a closer.
_LIST_HEADED_COMPOUNDS = frozenset({"if", "until", "while"})

#: Words that precede a command word without being one, so command position survives them.
_COMMAND_POSITION_WORDS = frozenset({"!", "do", "elif", "else", "then", "time"})

#: Operator spellings, longest first — the order IS the tokenisation. ``|&`` is listed as
#: unrecognised rather than split, so its ``&`` can never be mistaken for a list terminator.
_OPERATORS: tuple[tuple[str, str], ...] = (
    (";;&", "control"),
    (";;", "control"),
    (";&", "control"),
    ("&>>", "redirection"),
    ("&>", "redirection"),
    ("&&", "control"),
    ("||", "control"),
    ("|&", "unknown"),
    ("<<<", "redirection"),
    ("<<-", "redirection"),
    ("<<", "redirection"),
    ("<&", "redirection"),
    ("<>", "redirection"),
    (">>", "redirection"),
    (">|", "redirection"),
    (">&", "redirection"),
    (";", "control"),
    ("&", "control"),
    ("|", "pipe"),
    ("<", "redirection"),
    (">", "redirection"),
    ("\n", "control"),
    ("(", "open-paren"),
    (")", "close-paren"),
)

#: Compounds delimited by a doubled bracket. Consumed whole and reported unrecognised, so a
#: ``;`` or ``>`` inside one is never read as an operator at the enclosing level.
_BRACKETED_COMPOUNDS = (("((", "))"), ("[[", "]]"))

#: A redirection target that is itself a command list. Consumed as one word, so its parens
#: never open a frame; :mod:`teatree.eval.command_span` blocks the data-only proof outright.
_PROCESS_SUBSTITUTION = ("<(", ">(")


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


@dataclass(frozen=True)
class _Token:
    kind: str
    start: int
    text: str


@dataclass
class _Frame:
    """One open compound, named by the token that closes it."""

    closer: str
    function_body: bool


def _bracketed_compound_end(text: str, index: int) -> int | None:
    """Index just past the ``(( … ))`` or ``[[ … ]]`` opening at *index*, or ``None``."""
    for opener, closer in _BRACKETED_COMPOUNDS:
        if text.startswith(opener, index):
            close = text.find(closer, index + len(opener))
            return len(text) if close == -1 else close + len(closer)
    return None


def _leading_operator(text: str, index: int) -> tuple[str, str] | None:
    """The operator spelling starting at *index* and its kind, or ``None`` for a word."""
    for spelling, kind in _OPERATORS:
        if text.startswith(spelling, index):
            return spelling, kind
    return None


def _tokenise(text: str, start: int = 0) -> list[_Token]:
    """*text* from *start* as the tokens bash's grammar is written over.

    A ``#`` is a comment only at a token start, which is where bash starts a word — a
    mid-word one (``grep a#b``) stays a literal character, and reading it as a comment
    would hide the ``|`` after it from the pipeline split and turn a kept act into a
    dropped one.
    """
    tokens: list[_Token] = []
    index = start
    while index < len(text):
        char = text[index]
        if char in " \t":
            index += 1
            continue
        if char == "#":
            newline = text.find("\n", index)
            end = len(text) if newline == -1 else newline
            tokens.append(_Token("comment", index, text[index:end]))
            index = end
            continue
        if text.startswith(_PROCESS_SUBSTITUTION, index):
            close = matching_paren(text, index + 1)
            end = len(text) if close is None else close + 1
            tokens.append(_Token("word", index, text[index:end]))
            index = end
            continue
        bracketed = _bracketed_compound_end(text, index)
        if bracketed is not None:
            tokens.append(_Token("unknown", index, text[index:bracketed]))
            index = bracketed
            continue
        operator = _leading_operator(text, index)
        if operator is not None:
            spelling, kind = operator
            tokens.append(_Token(kind, index, spelling))
            index += len(spelling)
            continue
        end = max(word_end(text, index), index + 1)
        tokens.append(_Token("word", index, text[index:end]))
        index = end
    return tokens


class Recogniser:
    r"""The compound commands a left-to-right scan is inside, and whether it understood them.

    One ORDERED frame stack holds every open compound, group and keyword alike. The split
    stack the enumerating scan needed is gone with the hazard that motivated it: a stray
    ``fi`` cancelling a real brace could once end the window EARLIER than a group-only scan,
    the one direction that newly drops an act. A closer matching no open frame is now
    unrecognised, and unrecognised widens, so the mismatch cannot narrow anything.

    ``unrecognised`` is sticky and one-way. It is the whole guarantee: :func:`segment_end`
    refuses to bound a window that carries it, so a construct outside the accept table costs
    an over-keep rather than a silent elision.
    """

    def __init__(self) -> None:
        self.frames: list[_Frame] = []
        self.unrecognised = False
        self._command_position = True
        self._pending_function_body = False
        self._expect_redirection_target = False
        self._naming_a_function = False

    def in_a_function_body(self) -> bool:
        """Whether any open frame is a function body, whose stdout flows to the call site."""
        return any(frame.function_body for frame in self.frames)

    def closed(self) -> bool:
        """Whether a control operator at the cursor ends the enclosing command list."""
        return not self.frames

    def feed(self, tokens: list[_Token], index: int) -> int:
        """Consume the token at *index*, returning the index of the next one."""
        token = tokens[index]
        if token.kind == "comment":
            return index + 1
        if self._pending_function_body and not self._resolves_a_function_body(token):
            return index + 1
        if self._expect_redirection_target:
            self._expect_redirection_target = False
            self.unrecognised = self.unrecognised or token.kind != "word"
            return index + 1
        if self._naming_a_function:
            return self._name_a_function(tokens, index)
        return self._feed_by_kind(tokens, index)

    def _feed_by_kind(self, tokens: list[_Token], index: int) -> int:
        token = tokens[index]
        if token.kind == "redirection":
            self._expect_redirection_target = True
        elif token.kind in {"control", "pipe"}:
            self._command_position = True
        elif token.kind == "unknown":
            self.unrecognised = True
        elif token.kind == "open-paren":
            self._open_a_subshell()
        elif token.kind == "close-paren":
            self._close_a_paren()
        else:
            return self._feed_word(tokens, index)
        return index + 1

    def _resolves_a_function_body(self, token: _Token) -> bool:
        """Whether *token* is the compound a function definition header demands.

        bash allows a newline between the header and the body and nothing else, so any
        other token is a syntax error there — and a header this scan cannot resolve leaves
        the body frame unmarked, which is what would let a call-site pipe go unseen.
        """
        if token.kind == "control" and token.text == "\n":
            return True
        opens = token.kind == "open-paren" or (token.kind == "word" and token.text in {"{", *_COMPOUND_CLOSER})
        if not opens:
            self.unrecognised = True
            self._pending_function_body = False
        return opens

    def _name_a_function(self, tokens: list[_Token], index: int) -> int:
        """Consume the name after ``function``, and the optional ``()`` following it."""
        self._naming_a_function = False
        if tokens[index].kind != "word":
            self.unrecognised = True
            return index + 1
        self._pending_function_body = True
        self._command_position = True
        return index + 1 + _empty_parens_at(tokens, index + 1)

    def _open_a_subshell(self) -> None:
        if self._command_position:
            self._push(")")
        else:
            self.unrecognised = True

    def _close_a_paren(self) -> None:
        """Pop a subshell, or terminate a ``case`` PATTERN, which closes no frame at all.

        ``{ case x in x) …;; esac; } | bash`` would otherwise have the brace frame popped by
        a pattern, narrowing the window short of the pipe.
        """
        if self.frames and self.frames[-1].closer == ")":
            self.frames.pop()
            self._command_position = False
        elif self.frames and self.frames[-1].closer == "esac":
            self._command_position = True
        else:
            self.unrecognised = True

    def _feed_word(self, tokens: list[_Token], index: int) -> int:
        word = tokens[index].text
        if self._command_position and word not in _COMMAND_POSITION_WORDS:
            return self._feed_keyword_or_name(tokens, index, word)
        return index + 1

    def _feed_keyword_or_name(self, tokens: list[_Token], index: int, word: str) -> int:
        """A word where bash reads a command name: a keyword, a closer, or the name itself."""
        if word == "function":
            self._naming_a_function = True
        elif word == "{":
            self._push("}")
        elif word in _CLOSER_WORDS:
            self._close_a_compound(word)
        elif word in _COMPOUND_CLOSER:
            self._push(_COMPOUND_CLOSER[word])
            self._command_position = word in _LIST_HEADED_COMPOUNDS
        else:
            return self._feed_command_word(tokens, index)
        return index + 1

    def _feed_command_word(self, tokens: list[_Token], index: int) -> int:
        """A plain word in command position — the command name, or a definition header.

        The name of ``name ( ) compound_command`` is whatever token sits before the parens,
        so ``f+g``, ``f[g]`` and ``f!g`` are headers exactly as ``f`` is; a character class
        naming what a name may contain drops every spelling it did not think of.
        """
        parens = _empty_parens_at(tokens, index + 1)
        self._pending_function_body = bool(parens)
        self._command_position = bool(parens)
        return index + 1 + parens

    def _close_a_compound(self, word: str) -> None:
        if self.frames and self.frames[-1].closer == word:
            self.frames.pop()
            self._command_position = False
        else:
            self.unrecognised = True

    def _push(self, closer: str) -> None:
        self.frames.append(_Frame(closer, self._pending_function_body))
        self._pending_function_body = False
        self._command_position = True


def _empty_parens_at(tokens: list[_Token], index: int) -> int:
    """``2`` where *tokens* holds the ``( )`` of a function-definition header, else ``0``."""
    pair = tokens[index : index + 2]
    return 2 if [token.kind for token in pair] == ["open-paren", "close-paren"] else 0


def enclosing(text: str, end: int) -> Recogniser:
    """The compound nesting still open just before *end*, and whether it was understood."""
    recogniser = Recogniser()
    tokens = _tokenise(text[:end])
    index = 0
    while index < len(tokens):
        index = recogniser.feed(tokens, index)
    return recogniser


def segment_end(text: str, start: int, recogniser: Recogniser) -> int:
    """Index just past every program the text redirected at *start* can reach.

    *recogniser* is what encloses *start*, so the scan runs past those compounds' own
    terminators and closers to the outer pipeline: their stdout is their contents' stdout,
    which is what carries the redirected text out of ``{ … }`` or ``if … fi`` into ``| bash``.
    An unterminated compound leaves a frame open, so the window runs to end of text.

    A bound is returned only where every token up to it was positively recognised. That is
    the fail-closed default: a construct this module does not model leaves the window
    unbounded, so the redirected text is kept rather than silently elided.
    """
    tokens = _tokenise(text, start)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index = recogniser.feed(tokens, index)
        if recogniser.unrecognised:
            return len(text)
        if recogniser.closed() and token.kind == "control":
            return token.start
    return len(text)
