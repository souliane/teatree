"""Refuse a command that names ``void-merge`` unless every mention is a standalone read-only ``t3`` call.

An inner shell runs quoted text (``bash -c``, ``script -c``, ``ssh``, a heredoc), so a mention
counts wherever it sits, literal or once the outer quoting is decoded. A word built at run time or
through an inner shell's own decoding is not seen (BLUEPRINT "Merge void" lists the gaps); the
command's own operator checks still refuse it.
"""

import re

from teatree.hooks._shell_lexer import Token, split_commands, tokenize
from teatree.hooks.forge_subcommand import basename, command_substitution_bodies

_SUBCOMMAND = "void-merge"
_MENTION = re.compile(r"(?<![\w-])void-merge(?![\w-])")
_QUOTING = re.compile(r"\\\r?\n|['\"\\]")
_READ_ONLY_FLAGS = frozenset({"--dry-run", "--help"})
# Bash rewrites such a word before the program reads it: an expansion, a glob, a brace list, a redirection.
_REWRITTEN = re.compile(r"[$`*?\[{<>]")

DENY_REASON = (
    "BLOCKED: `pr void-merge` rewrites a recorded merge, so only a human at a terminal runs it. "
    "Ask the operator to type `t3 <overlay> pr void-merge <pr-url> --reason '<why>' --by <their name>` "
    "in their own shell; it shows the forge evidence and asks them to type yes. "
    "Only a standalone `t3 <overlay> pr void-merge <pr-url> --dry-run` (or `--help`) runs here: any other "
    "command naming void-merge is refused, a grep or an echo included, because an inner shell could run "
    "that text. To search, use the Grep tool or a pattern such as `void.merge`; in a commit message or note, "
    "write `merge void` or `void.merge`, or pass the message as a file with `git commit -F <message file>`."
)


def void_merge_deny_reason(command: str) -> str | None:
    standalone = sum(_is_read_only_call(segment) for segment in split_commands(tokenize(command)))
    literal = len(_MENTION.findall(_QUOTING.sub("", command)))
    return None if literal == _decoded_mentions(command) == standalone else DENY_REASON


def _decoded_mentions(command: str) -> int:
    in_words = sum(len(_MENTION.findall(token.value)) for token in tokenize(command))
    return in_words + sum(_decoded_mentions(body) for body in command_substitution_bodies(command))


def _is_read_only_call(segment: list[Token]) -> bool:
    match segment:
        case [program, overlay, Token(value="pr"), Token(raw="void-merge"), *tail]:
            literal = not _REWRITTEN.search(program.raw + overlay.raw)
            return literal and basename(program.value) == "t3" and _flag_is_parsed(tail)
        case _:
            return False


def _flag_is_parsed(tail: list[Token]) -> bool:
    previous = _SUBCOMMAND
    for token in tail:
        if _REWRITTEN.search(token.raw) or token.value == "--":
            return False
        if token.value in _READ_ONLY_FLAGS and not _may_take_a_value(previous):
            return True
        previous = token.value
    return False


def _may_take_a_value(word: str) -> bool:
    return word.startswith("-") and "=" not in word and word not in _READ_ONLY_FLAGS
