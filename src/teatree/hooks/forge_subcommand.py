"""Action-aware detection of a forge CLI subcommand invoked as an executed program.

A forge subcommand phrase (``gh pr merge``, ``glab mr create``) appears in plenty of text
that never runs it — a heredoc body, a quoted ``echo`` operand, a ``#`` comment. Matching
the phrase as a SUBSTRING blocks those too (the #1415 content-not-action over-block class),
so the raw-MERGE and raw-CREATE detectors both ask the narrower question: is the subcommand
the EXECUTED PROGRAM of a command segment?

One implementation answers it for both, keyed on a per-caller ``{program: (subword, …)}``
table, so the two gates can never drift on which invocation forms they recognise. It errs
toward BLOCK: a leading ``NAME=val`` env run, a known argv wrapper, a path-qualified program
word, shell grouping/compound keywords, and command substitutions are all descended through,
so any plausible invocation fires. Only a heredoc body (stripped), a comment (dropped by the
lexer), and a quoted-string operand (a non-command-position token) pass. A substitution runs
wherever bash expands one: unquoted, in double quotes, and in a heredoc whose delimiter is unquoted.

Stdlib-only apart from the sibling lexer, so Lane B and the cold PreToolUse subprocess can
both import it.
"""

import re
import shlex
from collections.abc import Collection, Mapping
from dataclasses import dataclass

from teatree.hooks._shell_lexer import split_commands, tokenize

# A heredoc head ending its line: ``<<['"]?DELIM['"]?\n`` (never a ``<<<`` here-string). Its body,
# up to the line that is exactly DELIM (tabs first stripped for ``<<-``), is stripped before lexing so
# a body line that BEGINS with the subcommand phrase cannot land at a command position.
_HEREDOC_HEAD_RE = re.compile(r"(?<!<)<<(-)?[ \t]*(['\"]?)(\w+)\2[ \t]*\n")

# Where a heredoc delimiter word ends, and what a ``#`` must follow to open a comment: the start of a
# word, never a ``)`` closing a substitution (``$(git rev-parse HEAD)#L10`` is one word).
_WORD_BREAKS = frozenset(" \t\n;&|()<>")
_COMMENT_AFTER = frozenset(" \t\n;&|(")

# A leading ``NAME=val`` env-assignment run (consumed before the program word).
_ENV_ASSIGN_RE = re.compile(r"^\w+=")

# Wrapper programs whose first non-option operand is the real executed program, each mapped
# to the options that consume the NEXT word as their value.
_WRAPPER_VALUE_OPTIONS: dict[str, frozenset[str]] = {
    "command": frozenset(),
    "nohup": frozenset(),
    "time": frozenset({"-f", "--format", "-o", "--output"}),
    "exec": frozenset({"-a"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "xargs": frozenset({"-n", "-L", "-I", "-P", "-s", "-d", "-E", "-a", "--max-args", "--max-procs", "--delimiter"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "stdbuf": frozenset({"-i", "--input", "-o", "--output", "-e", "--error"}),
    "setsid": frozenset(),
}

# Wrappers whose own operands come before the program (``timeout DURATION gh …``): how many.
_WRAPPER_OPERANDS: dict[str, int] = {"timeout": 1}

# Shell grouping / compound keywords that PRECEDE a command word in a segment.
_COMPOUND_KEYWORDS: frozenset[str] = frozenset(
    {"(", ")", "{", "}", "if", "then", "else", "elif", "fi", "do", "done", "while", "until", "case", "esac", "!"},
)

# The gh/glab HTTP-method flag, both empirically-valid forms: spaced/``=`` (``-X PUT``,
# ``--method=POST``) and the no-space pflag shorthand (``-XPUT``, a real override that the
# spaced-only pattern missed). Consumers flatten the two groups and keep last-wins semantics.
_METHOD_FLAG_RE = re.compile(r"(?:-X|--method)[\s=]+['\"]?([A-Za-z]+)\b|(?<=-X)([A-Za-z]+)\b")
_BODY_FLAG_RE = re.compile(r"(?:^|\s)(?:--(?:field|raw-field|input|data)\b|-[fFd])")

# ``gh``/``glab api`` options that take the next word as their value, and those among them
# that carry a request body (which makes the forge default the method to POST).
_API_VALUE_OPTIONS: frozenset[str] = frozenset(
    {
        "-X",
        "--method",
        "-H",
        "--header",
        "-f",
        "-F",
        "--field",
        "--raw-field",
        "--input",
        "-d",
        "--data",
        "-q",
        "--jq",
        "-t",
        "--template",
        "-p",
        "--preview",
        "--hostname",
        "--cache",
        "--output",
    }
)
_API_BODY_OPTIONS: frozenset[str] = frozenset({"-f", "-F", "--field", "--raw-field", "--input", "-d", "--data"})
_FORGE_PROGRAMS: frozenset[str] = frozenset({"gh", "glab"})
_SPLIT_STRING_OPTIONS: frozenset[str] = frozenset({"-S", "--split-string"})

# A ``gh``/``glab api`` REST call — the out-of-band mutation surface both gates watch.
GLAB_GH_API_RE = re.compile(r"\b(?:glab|gh)\s+api\b")


def effective_method_is_write(command: str) -> bool:
    """Whether a gh/glab REST command's EFFECTIVE HTTP method is a write (not GET).

    The LAST ``-X``/``--method`` value wins; with no method flag the forge defaults to POST
    when a body/field flag is present, else GET. A GET is the only read.
    """
    methods = [m.upper() for pair in _METHOD_FLAG_RE.findall(command) for m in pair if m]
    if methods:
        return methods[-1] != "GET"
    return bool(_BODY_FLAG_RE.search(command))


def basename(word: str) -> str:
    """The final path component of a program word (``/usr/bin/gh`` → ``gh``)."""
    return word.rsplit("/", 1)[-1]


def strip_heredoc_bodies(command: str) -> str:
    """Remove heredoc body content, keeping the redirect head and the delimiter line.

    A line opening more than one heredoc, and everything after it, is left to the lexer, which reads
    their bodies in turn: stripping from the last head would remove the first one's delimiter line.
    """
    kept: list[str] = []
    index = 0
    while (head := _HEREDOC_HEAD_RE.search(command, index)) is not None:
        delimiter_line = _delimiter_line(command, head.end(), head[3], strip_tabs=bool(head[1]))
        if delimiter_line is None or command[command.rfind("\n", 0, head.start()) + 1 : head.end()].count("<<") > 1:
            break
        kept.append(command[index : head.end()])
        index = delimiter_line
    return "".join(kept) + command[index:]


def _delimiter_line(text: str, index: int, delimiter: str, *, strip_tabs: bool) -> int | None:
    """Where the line that ends a heredoc body starting at *index* begins; ``None`` when no line does."""
    while index <= len(text):
        newline = text.find("\n", index)
        line = text[index : len(text) if newline < 0 else newline]
        if (line.lstrip("\t") if strip_tabs else line) == delimiter:
            return index
        if newline < 0:
            return None
        index = newline + 1
    return None


def command_substitution_bodies(command: str) -> list[str]:
    """Return the inner text of every LIVE ``$(…)``, backtick and ``<(…)`` / ``>(…)`` substitution.

    A subcommand invoked inside a substitution still executes, so each body is fed back through the
    detector; a nested one is captured whole inside its outer body. The walk reads the text the way
    bash does: single quotes, a ``#`` comment and the body of a heredoc whose delimiter is quoted
    (``<<'EOF'``) are literal text bash never expands, so a span there is skipped — and an apostrophe
    in one never shifts the quoting of what follows. An unquoted heredoc body expands ``$(…)`` and
    backticks, while its quotes are plain characters.
    """
    bodies: list[str] = []
    _live_text(command, 0, bodies, closing=False)
    return bodies


def _live_text(text: str, index: int, bodies: list[str], *, closing: bool) -> int:
    """Walk shell text from *index*, adding each live substitution's body to *bodies*.

    Inside a ``$(`` (*closing*), the walk returns the index of the ``)`` closing it, else ``len(text)``.
    """
    depth = 0
    in_single = in_double = False
    heredocs: list[tuple[str, bool, bool]] = []
    while index < len(text):
        char = text[index]
        if in_single:
            in_single = char != "'"
            index += 1
            continue
        if char == "\\":
            index += 2
            continue
        span = _substitution_span(text, index, process=not in_double)
        if span is not None:
            bodies.append(span[0])
            index = span[1]
            continue
        if char == '"' or in_double:
            in_double = in_double != (char == '"')
        elif char == "'":
            in_single = True
        elif (unexpanded_end := _past_unexpanded(text, index, heredocs, bodies)) is not None:
            index = unexpanded_end
            continue
        elif closing and char in "()":
            depth += 1 if char == "(" else -1
            if depth < 0:
                return index
        index += 1
    return index


def _past_unexpanded(text: str, index: int, heredocs: list[tuple[str, bool, bool]], bodies: list[str]) -> int | None:
    """The index past a ``#`` comment, an ANSI-C ``$'…'`` string, a heredoc head, or the bodies a newline starts.

    ``None`` when none of them starts at *index*. A head is queued on *heredocs* until its line ends.
    """
    if text[index] == "#" and (index == 0 or text[index - 1] in _COMMENT_AFTER):
        return _line_end(text, index)
    if text.startswith("$'", index):
        cursor = index + 2
        while cursor < len(text) and text[cursor] != "'":
            cursor += 2 if text[cursor] == "\\" else 1
        return cursor + 1
    if (head := _heredoc_head(text, index)) is not None:
        heredocs.append(head[0])
        return head[1]
    if text[index] == "\n" and heredocs:
        end = _heredoc_bodies(text, index + 1, heredocs, bodies)
        heredocs.clear()
        return end
    return None


def _substitution_span(text: str, start: int, *, process: bool) -> tuple[str, int] | None:
    """``(body, index after the span)`` for a substitution opening at *start*, else ``None``.

    ``$(…)`` everywhere it is live; ``<(…)`` / ``>(…)`` only where *process* substitution applies.
    """
    if text.startswith("$(", start) or (process and text.startswith(("<(", ">("), start)):
        end = _live_text(text, start + 2, [], closing=True)
        return text[start + 2 : end], min(end + 1, len(text))
    if text[start] == "`":
        j = start + 1
        while j < len(text) and text[j] != "`":
            j += 2 if text[j] == "\\" else 1
        return text[start + 1 : j], j + 1
    return None


def _line_end(text: str, index: int) -> int:
    newline = text.find("\n", index)
    return len(text) if newline < 0 else newline


def _heredoc_head(text: str, index: int) -> tuple[tuple[str, bool, bool], int] | None:
    """``((delimiter, strip_tabs, quoted), index after it)`` for a ``<<`` opening at *index*, else ``None``."""
    if not text.startswith("<<", index) or text.startswith("<<<", index) or text[index - 1 : index] == "<":
        return None
    cursor = index + 2
    strip_tabs = text.startswith("-", cursor)
    cursor += strip_tabs
    while text[cursor : cursor + 1] in {" ", "\t"}:
        cursor += 1
    delimiter: list[str] = []
    quoted = False
    while cursor < len(text) and text[cursor] not in _WORD_BREAKS:
        if text[cursor] in "'\"":
            close = text.find(text[cursor], cursor + 1)
            close = len(text) if close < 0 else close
            delimiter.append(text[cursor + 1 : close])
            cursor, quoted = close + 1, True
        else:
            quoted = quoted or text[cursor] == "\\"
            cursor += 2 if text[cursor] == "\\" else 1
            delimiter.append(text[cursor - 1 : cursor])
    return (("".join(delimiter), strip_tabs, quoted), cursor) if delimiter else None


def _heredoc_bodies(text: str, index: int, heredocs: list[tuple[str, bool, bool]], bodies: list[str]) -> int:
    """Read each pending heredoc body from *index*; an unquoted one's substitutions run. The index after them."""
    for delimiter, strip_tabs, quoted in heredocs:
        end = _delimiter_line(text, index, delimiter, strip_tabs=strip_tabs)
        body = text[index : len(text) if end is None else end]
        index = len(text) if end is None else min(_line_end(text, end) + 1, len(text))
        if not quoted:
            _expanded_body(body, bodies)
    return index


def _expanded_body(body: str, bodies: list[str]) -> None:
    """Add the substitutions of an unquoted heredoc *body*: its quotes are plain text, a backslash still escapes."""
    cursor = 0
    while cursor < len(body):
        if body[cursor] == "\\":
            cursor += 2
        elif (span := _substitution_span(body, cursor, process=False)) is not None:
            bodies.append(span[0])
            cursor = span[1]
        else:
            cursor += 1


def program_words(segment_words: list[str]) -> list[str]:
    """Return the words from the program word onward, env/wrapper/compound prefixes stripped.

    Consumes a leading ``NAME=val`` env run, leading shell grouping/compound keywords, and every
    wrapper prefix together with that wrapper's own options (``env -i``, ``time -p``,
    ``command --``, ``xargs -n 1``). ``env -S <string>`` executes the command that string
    splits into, so it is spliced in and walked in turn. The first remaining word is the
    executed program; the basename test is applied by the caller.
    """
    index = 0
    while index < len(segment_words):
        word = segment_words[index]
        if word in {"case", "function", "coproc"}:
            index = _past_head(segment_words, index)
            continue
        if _ENV_ASSIGN_RE.match(word) or word in _COMPOUND_KEYWORDS or word.endswith(")"):
            index += 1
            continue
        value_options = _WRAPPER_VALUE_OPTIONS.get(basename(word))
        if value_options is None:
            break
        index, split_string = _skip_wrapper_options(segment_words, index + 1, value_options)
        if split_string is not None:
            return program_words(_split_command_string(split_string) + segment_words[index:])
        index += _WRAPPER_OPERANDS.get(basename(word), 0)
    return segment_words[index:]


def _past_head(words: list[str], index: int) -> int:
    """The index past ``case WORD in``, ``function NAME`` or ``coproc`` (and its ``NAME`` before a ``{``).

    A case arm's ``PATTERN)`` and a function's ``NAME()`` are then skipped as words ending in ``)``, so the
    arm's or the body's command is the program.
    """
    if words[index] == "function":
        return index + 2
    if words[index] == "coproc":
        return index + (2 if words[index + 2 : index + 3] == ["{"] else 1)
    return words.index("in", index) + 1 if "in" in words[index:] else len(words)


def _skip_wrapper_options(words: list[str], index: int, value_options: frozenset[str]) -> tuple[int, str | None]:
    """The index after a wrapper's options (``--`` ends them), and any ``env -S`` string they carry."""
    split_string: str | None = None
    while index < len(words) and words[index].startswith("-") and len(words[index]) > 1:
        name, glued = _split_option(words[index])
        takes_next = glued is None and name in value_options
        if name in _SPLIT_STRING_OPTIONS:
            split_string = glued if glued is not None else (words[index + 1] if index + 1 < len(words) else "")
        index += 2 if takes_next else 1
        if name == "--":
            break
    return index, split_string


def _split_command_string(text: str) -> list[str]:
    """``env -S``'s own word splitting — an unbalanced quote falls back to whitespace, never to nothing."""
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def segment_word_lists(command: str) -> list[list[str]]:
    """Lex *command* into per-segment WORD-value lists (comments and quotes resolved)."""
    return [[token.value for token in segment] for segment in split_commands(tokenize(command))]


def invokes_forge_subcommand(command: str, subwords: Mapping[str, tuple[str, ...]]) -> bool:
    """Whether *command* INVOKES one of *subwords*' forge subcommands as an executed program.

    *subwords* maps a forge program (``gh``, ``glab``) to the subcommand words that must
    follow it (``("pr", "merge")``), so one traversal serves every gate keyed on this shape.
    """
    return bool(forge_subcommand_argvs(command, subwords))


def forge_subcommand_argvs(command: str, subwords: Mapping[str, tuple[str, ...]]) -> list[list[str]]:
    """The argv after the subcommand words, for every EXECUTED invocation of one of *subwords*."""
    return [
        words[1 + len(expected) :]
        for words in forge_program_argvs(command, subwords.keys())
        if tuple(words[1 : 1 + len(expected := subwords[basename(words[0])])]) == expected
    ]


def forge_program_argvs(command: str, programs: Collection[str]) -> list[list[str]]:
    """The words from the program word on, for every EXECUTED command segment whose program is one of *programs*."""
    if not command:
        return []
    argvs = [
        words
        for segment in segment_word_lists(strip_heredoc_bodies(command))
        if (words := program_words(list(segment))) and basename(words[0]) in programs
    ]
    for body in command_substitution_bodies(command):
        argvs.extend(forge_program_argvs(body, programs))
    return argvs


@dataclass(frozen=True, slots=True)
class ApiCall:
    """One ``gh``/``glab api`` invocation, read from its argv rather than from the raw text."""

    endpoint: str | None
    method: str | None
    has_body: bool

    @property
    def is_write(self) -> bool:
        """The last explicit method wins; with none, a body makes the forge default to POST."""
        return self.method != "GET" if self.method else self.has_body


def forge_api_calls(command: str) -> list[ApiCall]:
    """Every ``gh``/``glab api`` call *command* EXECUTES — at a command position or in a substitution.

    Text that only mentions an API call (a quoted operand, a heredoc body, a comment) is not
    one, so it yields nothing.
    """
    calls = [
        _parse_api_argv(words[2:])
        for segment in segment_word_lists(strip_heredoc_bodies(command))
        if _is_forge_api(words := program_words(list(segment)))
    ]
    for body in command_substitution_bodies(command):
        calls.extend(forge_api_calls(body))
    return calls


def _is_forge_api(words: list[str]) -> bool:
    return words[1:2] == ["api"] and basename(words[0]) in _FORGE_PROGRAMS


def _parse_api_argv(argv: list[str]) -> ApiCall:
    endpoint: str | None = None
    method: str | None = None
    has_body = False
    words = iter(argv)
    for word in words:
        name, value = _split_option(word)
        if name is None:
            endpoint = endpoint or word
            continue
        if value is None and name in _API_VALUE_OPTIONS:
            value = next(words, "")
        if name in {"-X", "--method"}:
            method = (value or "").upper()
        elif name in _API_BODY_OPTIONS:
            has_body = True
    return ApiCall(endpoint=endpoint, method=method, has_body=has_body)


def _split_option(word: str) -> tuple[str | None, str | None]:
    """``(name, glued value)`` for an option word — ``-XPOST``, ``-ftitle=x``, ``--method=PUT``."""
    if word.startswith("--") and word != "--":
        name, sep, value = word.partition("=")
        return name, value if sep else None
    if word.startswith("-") and word != "-":
        return word[:2], word[2:].removeprefix("=") or None
    return None, None


__all__ = [
    "GLAB_GH_API_RE",
    "ApiCall",
    "basename",
    "command_substitution_bodies",
    "effective_method_is_write",
    "forge_api_calls",
    "forge_program_argvs",
    "forge_subcommand_argvs",
    "invokes_forge_subcommand",
    "program_words",
    "segment_word_lists",
    "strip_heredoc_bodies",
]
