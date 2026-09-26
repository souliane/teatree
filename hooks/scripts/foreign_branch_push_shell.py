"""The shell structure the foreign-branch push gate must see before it may judge.

The gate used to read a command as one flat token list, so a real newline, a
backslash-newline continuation, a ``{ }`` group, a control word, a redirection,
a ``bash -lc`` cluster, a ``timeout`` wrapper and a ``#`` comment each hid a
real push from it — measured over five weeks of traffic, 194 of 329 real pushes
were invisible.

Mechanics only, no policy: this leaf answers "what did the shell actually run,
and what could I not read", and the gate decides what to do with each answer.
Same split as :mod:`foreign_branch_push_git`.

Flattening is deliberately narrow. Only what shell semantics make CERTAIN is
resolved; a wrapper whose argv shape would need its own option table (``sudo``,
``xargs``, ``env -i``) is left unresolved for the gate to refuse, because a rule
that guesses is a rule that judges the wrong branch.

Cold-import safe at module top: stdlib plus one dependency-free sibling.
"""

import re
import shlex
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

from hooks.scripts.mr_cli_fields import _HEREDOC_RE

# A real newline joins shlex's own separators, so a line break cuts a segment
# instead of vanishing into the whitespace between two words.
_PUNCTUATION: Final[str] = "();<>|&\n"
_WORD_SEPARATORS: Final[str] = " \t\r"
_QUOTES: Final[str] = "'\""
# What an unquoted ``#`` must follow to open a comment. Deliberately short:
# over-stripping DROPS a push, where under-stripping only refuses one.
_WORD_BOUNDARY: Final[str] = " \t\r\n;&|"
_ENV_ASSIGNMENT_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
_CONTINUATION_RE: Final[re.Pattern[str]] = re.compile(r"\\\n")
_HEREDOC_OPENER_RE: Final[re.Pattern[str]] = re.compile(r"^<<-?\s*(['\"]?)\w+\1")
_GIT_WORD_RE: Final[re.Pattern[str]] = re.compile(r"\bgit\b")
_PUSH_WORD_RE: Final[re.Pattern[str]] = re.compile(r"\bpush\b")

# Prefixes that run the NEXT word rather than being the command themselves.
_WRAPPER_LEADERS: Final[frozenset[str]] = frozenset(
    {"command", "env", "exec", "nohup", "time", "stdbuf", "nice", "sudo"}
)
# Shell keywords that introduce a command rather than being one.
_CONTROL_WORDS: Final[frozenset[str]] = frozenset(
    {
        "{",
        "}",
        "(",
        ")",
        "!",
        "if",
        "then",
        "else",
        "elif",
        "fi",
        "while",
        "until",
        "do",
        "done",
        "case",
        "esac",
        "coproc",
    }
)
# ``timeout`` is the only arity-aware leader: real traffic wraps pushes in it 91
# times and in nothing else, so no other wrapper earns an option table.
_TIMEOUT_VALUE_FLAGS: Final[frozenset[str]] = frozenset({"-s", "-k", "--signal", "--kill-after"})

FORCE_FLAGS: Final[frozenset[str]] = frozenset({"-f", "--force", "--force-with-lease", "--force-if-includes"})
_SHORT_CLUSTER_LENGTH: Final[int] = 3


@dataclass(frozen=True, slots=True)
class Heredoc:
    """A heredoc body and the command words of the line that consumes it.

    The words rather than the leader: a wrapper hands the body on to something
    else (``docker exec -i box sh``), and only the caller knows which of them
    EXECUTES it.
    """

    consumer: tuple[str, ...]
    body: str


@dataclass(frozen=True, slots=True)
class Flattened:
    """What the shell would run, plus whether the text could be lexed at all."""

    segments: tuple[tuple[str, ...], ...]
    # ``piped_after[i]`` is True iff a literal ``|`` (not ``;``/``&&``/a
    # newline) joins ``segments[i]`` to the one before it — the only join a
    # payload-less shell actually reads its predecessor's stdout through.
    piped_after: tuple[bool, ...]
    heredocs: tuple[Heredoc, ...]
    unparsable: bool


def flatten_command(command: str) -> Flattened:
    """*command* as executable segments, its heredocs kept aside for the caller."""
    heredocs = tuple(_heredocs(command))
    tokens = tokenize(_without_heredoc_bodies(command))
    if tokens is None:
        return Flattened(segments=(), piped_after=(), heredocs=heredocs, unparsable=True)
    segments, piped_after = _segments(tokens)
    return Flattened(segments=segments, piped_after=piped_after, heredocs=heredocs, unparsable=False)


def _without_heredoc_bodies(command: str) -> str:
    """*command* with each heredoc body dropped and its opener line's TAIL kept.

    A heredoc span runs from ``<<DELIM`` to the terminator, so removing the span
    whole takes the rest of the opener LINE with it — and everything after the
    redirect is ordinary shell: ``cat <<'EOF' && git push --force origin theirs``
    lost the push, which the gate then neither read nor recorded as unread.
    """
    return _HEREDOC_RE.sub(lambda match: f" {_opener_tail(match.group())}", command)


def _opener_tail(span: str) -> str:
    """What follows *span*'s ``<<DELIM`` redirect on its own line."""
    return _HEREDOC_OPENER_RE.sub("", span.partition("\n")[0])


def tokenize(text: str) -> list[str] | None:
    """Shell words plus separator tokens, or ``None`` when the quoting is unbalanced.

    There is no guessed fallback: a decision about who owns a branch must never
    rest on a tokenization the shell would not have produced.
    """
    lexer = shlex.shlex(_CONTINUATION_RE.sub("", _without_comments(text)), posix=True, punctuation_chars=_PUNCTUATION)
    lexer.whitespace = _WORD_SEPARATORS
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return None


def _without_comments(text: str) -> str:
    """*text* with the shell's own ``#`` comments and every IO_NUMBER dropped.

    An IO_NUMBER is a digit run glued to a redirect (``2>&1``); both jobs share
    one pass because both need the same quote/word-boundary tracking.
    ``shlex.commenters`` cannot do the comment half either: it reads a comment
    with ``readline()``, taking the newline that separates two commands with
    it, and it ends a word at a ``#`` the shell keeps inside one (``origin
    feature#123``). The IO_NUMBER half exists because ``tokenize`` cannot do it
    downstream: ``shlex`` starts a fresh token at every punctuation char
    regardless of whitespace, so ``2>&1`` and ``2 > file`` (a branch literally
    named ``2``, redirected) tokenize identically — only the RAW text still
    knows which one had a space, so an fd prefix must be stripped here or not
    at all.
    """
    kept: list[str] = []
    quote = ""
    at_word_start = True
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and quote != "'":
            kept.append(text[index : index + 2])
            index += 2
            at_word_start = False
            continue
        if char == "#" and not quote and at_word_start:
            newline = text.find("\n", index)
            if newline < 0:
                break
            index = newline
            continue
        if not quote and at_word_start and char.isdigit():
            end = index
            while end < len(text) and text[end].isdigit():
                end += 1
            if end < len(text) and text[end] in "<>":
                index = end
                continue
        if quote:
            quote = "" if char == quote else quote
        elif char in _QUOTES:
            quote = char
        kept.append(char)
        at_word_start = not quote and char in _WORD_BOUNDARY
        index += 1
    return "".join(kept)


def past_leaders(tokens: Sequence[str]) -> list[str]:
    """*tokens* with env assignments, control words and run-the-next-word wrappers dropped."""
    index = 0
    while index < len(tokens):
        word = tokens[index]
        if command_name(word) == "timeout":
            index = _past_timeout(tokens, index + 1)
        elif _ENV_ASSIGNMENT_RE.match(word) or word in _CONTROL_WORDS or command_name(word) in _WRAPPER_LEADERS:
            index += 1
        else:
            break
    return list(tokens[index:])


def shell_payload(tokens: Sequence[str], shell: int = 0) -> str:
    """The script the shell at index *shell* runs via ``-c``/``-lc``/``-ec``, or ``""``.

    The search starts AFTER the shell because a wrapper in front of it has a
    ``-c`` of its own: ``kubectl exec pod -c <container> -- sh -c '<script>'``
    would otherwise hand back the container name where the script should be. It
    stops at ``--`` for the same reason from the other side: what follows is
    positional, so ``sh -s -- -c foo`` runs STDIN and reading its ``-c`` as a
    payload would call an executed heredoc data.
    """
    for index in range(shell + 1, len(tokens) - 1):
        if tokens[index] == "--":
            return ""
        if tokens[index] == "-c" or short_cluster_has(tokens[index], "c"):
            return tokens[index + 1]
    return ""


def command_name(word: str) -> str:
    """*word*'s command name, keeping words :class:`PurePosixPath` reads as empty (``.``)."""
    return PurePosixPath(word).name or word


def names_git_push(tokens: Sequence[str]) -> bool:
    return any(command_name(word) == "git" for word in tokens) and "push" in tokens


def names_git_push_in_text(text: str) -> bool:
    return bool(_GIT_WORD_RE.search(text) and _PUSH_WORD_RE.search(text))


def _carries_force(tokens: Sequence[str]) -> bool:
    return has_flag(tokens, FORCE_FLAGS, "f") or any(token.startswith("+") for token in tokens)


def force_near_push(text: str) -> bool:
    """Whether a push named in *text* carries a force flag of its OWN.

    Scoped to the push's own words inside its own segment, falling back to its
    whole line where that line does not lex. A force refusal bypasses the shared
    fail-open chain every other refusal routes through, so what raises one is
    narrow: scanning all of *text* escalates on any unrelated ``+``-prefixed
    token (a diff body inside an executed heredoc), and scanning the whole
    segment reads the WRAPPER's flags (``docker compose -f <file>``, ``ssh -f``)
    as the push's. Lines are taken one at a time because a heredoc body carries
    its own delimiters, which ``flatten_command`` would strip as a heredoc.
    """
    for line in _CONTINUATION_RE.sub("", text).splitlines():
        flat = flatten_command(line)
        parts = [line] if flat.unparsable else [" ".join(segment) for segment in flat.segments]
        if any(_carries_force(_push_words(part)) for part in parts):
            return True
    return False


def _push_words(part: str) -> list[str]:
    """*part*'s words from the ``git`` that names a push on; empty when it names none."""
    words = part.split()
    for index, word in enumerate(words):
        if command_name(word) == "git" and "push" in words[index + 1 :]:
            return words[index:]
    return []


def has_flag(args: Sequence[str], flags: frozenset[str], letter: str) -> bool:
    """Whether *args* carries one of *flags*, or a short cluster holding *letter*."""
    return any(
        arg in flags
        or any(arg.startswith(f"{flag}=") for flag in flags if flag.startswith("--"))
        or short_cluster_has(arg, letter)
        for arg in args
    )


def short_cluster_has(arg: str, letter: str) -> bool:
    """Whether *arg* is a clustered short-flag group carrying *letter* (``-fu``)."""
    if not arg.startswith("-") or arg.startswith("--") or len(arg) < _SHORT_CLUSTER_LENGTH:
        return False
    return letter in arg[1:]


def _heredocs(command: str) -> Iterator[Heredoc]:
    for match in _HEREDOC_RE.finditer(command):
        line = command[: match.start()].rpartition("\n")[2]
        yield Heredoc(consumer=_consumer_of(line), body=_heredoc_body(match.group()))


def _heredoc_body(span: str) -> str:
    """*span* without its ``<<DELIM`` opener and its terminator, so it reads as shell."""
    return span.partition("\n")[2].rpartition("\n")[0]


def _consumer_of(line: str) -> tuple[str, ...]:
    """The command words of the line a heredoc hands its body to."""
    tokens = tokenize(line)
    if tokens is None:
        return ()
    segments, _piped_after = _segments(tokens)
    return tuple(past_leaders(segments[-1])) if segments else ()


def _segments(tokens: Sequence[str]) -> tuple[tuple[tuple[str, ...], ...], tuple[bool, ...]]:
    segments: list[list[str]] = [[]]
    piped_after: list[bool] = [False]
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not _is_separator(token):
            segments[-1].append(token)
        elif _is_redirection(token):
            index += 1  # the redirection target is an operand of the shell, not of the command
        else:
            segments.append([])
            piped_after.append(_is_pipe(token))
        index += 1
    return tuple(tuple(segment) for segment in segments), tuple(piped_after)


def _is_pipe(separator: str) -> bool:
    """Whether *separator* is a literal ``|`` — a real pipe, never ``||`` (logical-or)."""
    return separator == "|"


def _is_separator(token: str) -> bool:
    return bool(token) and not token[0].isalnum() and set(token) <= set(_PUNCTUATION)


def _is_redirection(token: str) -> bool:
    # Process substitution (`<(`, `>(`) glues a redirect char to `(` via the
    # same punctuation-run grouping — it opens a SUBSHELL, not a file target,
    # so treating it as one skips the subshell's own first word as "the
    # redirection target" and a `git` inside it vanishes before it is read.
    return ("<" in token or ">" in token) and "(" not in token


def _past_timeout(tokens: Sequence[str], index: int) -> int:
    while index < len(tokens) and tokens[index].startswith("-"):
        index += 2 if tokens[index] in _TIMEOUT_VALUE_FLAGS else 1
    return index + 1 if index < len(tokens) else index
