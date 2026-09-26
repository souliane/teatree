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
lexer), and a quoted-string operand (a non-command-position token) pass.

Stdlib-only apart from the sibling lexer, so Lane B and the cold PreToolUse subprocess can
both import it.
"""

import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass

from teatree.hooks._shell_lexer import split_commands, tokenize

# A heredoc body span: ``<<['"]?DELIM['"]?\n … \nDELIM``. Stripped before lexing so a body
# line that BEGINS with the subcommand phrase cannot land at a command position.
_HEREDOC_BODY_RE = re.compile(r"(<<-?\s*['\"]?\w+['\"]?\s*\n).*?(\n\s*\w+\b)", re.DOTALL)

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
}

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
    """Remove heredoc body content, keeping the redirect head and the delimiter line."""
    return _HEREDOC_BODY_RE.sub(lambda m: m.group(1) + m.group(2), command)


def command_substitution_bodies(command: str) -> list[str]:
    """Return the inner text of every LIVE ``$(…)`` and backtick command substitution.

    A subcommand invoked inside a substitution still executes, so each body is fed back
    through the detector. ``$(`` spans are matched by paren balance (nested substitutions are
    captured whole); backtick spans run to the next unescaped backtick. A span inside single
    quotes is literal text bash never expands, so it is skipped.
    """
    bodies: list[str] = []
    in_single = in_double = False
    i = 0
    while i < len(command):
        char = command[i]
        if char == "\\" and not in_single:
            i += 2
            continue
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif not in_single and (span := _substitution_span(command, i)) is not None:
            bodies.append(span[0])
            i = span[1]
            continue
        i += 1
    return bodies


def _substitution_span(command: str, start: int) -> tuple[str, int] | None:
    """``(body, index after the span)`` for a ``$(…)`` or backtick opening at *start*, else ``None``."""
    n = len(command)
    if command.startswith("$(", start):
        end = _closing_paren(command, start + 2)
        return command[start + 2 : end], min(end + 1, n)
    if command[start] == "`":
        j = start + 1
        while j < n and command[j] != "`":
            j += 2 if command[j] == "\\" else 1
        return command[start + 1 : j], j + 1
    return None


def _closing_paren(command: str, start: int) -> int:
    """The index of the ``)`` closing a ``$(`` whose body starts at *start* — quotes and escapes respected."""
    depth = 1
    in_single = in_double = False
    j = start
    while j < len(command):
        char = command[j]
        if char == "\\" and not in_single:
            j += 2
            continue
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif not (in_single or in_double) and char in "()":
            depth += 1 if char == "(" else -1
            if depth == 0:
                return j
        j += 1
    return len(command)


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
        if _ENV_ASSIGN_RE.match(word) or word in _COMPOUND_KEYWORDS:
            index += 1
            continue
        value_options = _WRAPPER_VALUE_OPTIONS.get(basename(word))
        if value_options is None:
            break
        index, split_string = _skip_wrapper_options(segment_words, index + 1, value_options)
        if split_string is not None:
            return program_words(_split_command_string(split_string) + segment_words[index:])
    return segment_words[index:]


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
    if not command:
        return []
    argvs = [
        argv
        for segment in segment_word_lists(strip_heredoc_bodies(command))
        if (argv := _segment_argv(list(segment), subwords)) is not None
    ]
    for body in command_substitution_bodies(command):
        argvs.extend(forge_subcommand_argvs(body, subwords))
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


def _segment_argv(segment_words: list[str], subwords: Mapping[str, tuple[str, ...]]) -> list[str] | None:
    """The argv after the subcommand when this segment executes one of *subwords*' verbs, else ``None``."""
    words = program_words(segment_words)
    if not words:
        return None
    expected = subwords.get(basename(words[0]))
    if expected is None or tuple(words[1 : 1 + len(expected)]) != expected:
        return None
    return words[1 + len(expected) :]


__all__ = [
    "GLAB_GH_API_RE",
    "ApiCall",
    "basename",
    "command_substitution_bodies",
    "effective_method_is_write",
    "forge_api_calls",
    "forge_subcommand_argvs",
    "invokes_forge_subcommand",
    "program_words",
    "segment_word_lists",
    "strip_heredoc_bodies",
]
