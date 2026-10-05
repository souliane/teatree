"""Whether a Bash command only inspects state — the reads a plan is written from.

Two independent answers must agree: the shared write-target resolver finds nothing
written (a redirect, ``sed -i``, a copy, an interpreter body), and every command in
the chain is an inspector. The leader set alone would pass ``cat a > b``; the write
resolver alone would pass ``git push``.
"""

import re
from typing import Final

from teatree.hooks._shell_lexer import Token, TokenKind, raw_substitution_sees_live, split_commands, tokenize
from teatree.hooks.write_targets import _GIT_VALUE_FLAGS, _leader_and_operands, bash_write_targets

_INSPECTORS: Final[frozenset[str]] = frozenset(
    {
        "basename",
        "cat",
        "cd",
        "cut",
        "diff",
        "dirname",
        "du",
        "egrep",
        "fgrep",
        "find",
        "grep",
        "head",
        "jq",
        "ls",
        "nl",
        "pwd",
        "readlink",
        "realpath",
        "rg",
        "stat",
        "tail",
        "wc",
        "which",
    }
)
_GIT_READS: Final[frozenset[str]] = frozenset(
    {
        "blame",
        "cat-file",
        "cherry",
        "describe",
        "diff",
        "fetch",
        "for-each-ref",
        "grep",
        "log",
        "ls-files",
        "ls-remote",
        "ls-tree",
        "merge-base",
        "rev-list",
        "rev-parse",
        "shortlog",
        "show",
        "show-ref",
        "status",
    }
)
_ACTING_OPTIONS: Final[dict[str, tuple[str, ...]]] = {
    "find": ("-delete", "-exec", "-ok", "-fls", "-fprint"),
    "rg": ("--pre",),
}
_GIT_CONFIG_OVERRIDES: Final[tuple[str, ...]] = ("-c", "--config-env", "--exec-path")
_SED_PRINT_SCRIPT_RE: Final[re.Pattern[str]] = re.compile(r"(?:(?:\d+|\$)(?:,(?:\d+|\$))?p;?)+")
_DISCARD_SINKS: Final[frozenset[str]] = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr"})
_SUBSTITUTION_OPENERS: Final[tuple[str, ...]] = ("$(", "`", "<(", ">(")


def is_read_only(command: str) -> bool:
    writes = bash_write_targets(command)
    if writes.unresolved or set(writes.targets) - _DISCARD_SINKS:
        return False
    segments = [
        [token for token in segment if token.kind is TokenKind.WORD] for segment in split_commands(tokenize(command))
    ]
    return bool(segments) and all(_inspects(words) for words in segments)


def _inspects(words: list[Token]) -> bool:
    if any(raw_substitution_sees_live(word.raw, _SUBSTITUTION_OPENERS) for word in words):
        return False
    leader, operands = _leader_and_operands(words)
    if leader == "git":
        return _git_reads(operands[1:])
    if leader == "sed":
        return _sed_prints_lines(operands[1:])
    acting = _ACTING_OPTIONS.get(leader, ())
    return leader in _INSPECTORS and not any(word.value.startswith(acting) for word in operands)


def _sed_prints_lines(args: list[Token]) -> bool:
    scripts: list[str] = []
    values = iter(word.value for word in args)
    for value in values:
        if value in {"-e", "--expression"}:
            scripts.append(next(values, ""))
        elif value.startswith("--expression="):
            scripts.append(value.partition("=")[2])
        elif value in {"-f", "--file"} or value.startswith("--file="):
            return False
        elif not value.startswith("-") and not scripts:
            scripts.append(value)
    return bool(scripts) and all(_SED_PRINT_SCRIPT_RE.fullmatch(script.strip()) for script in scripts)


def _git_reads(args: list[Token]) -> bool:
    cursor = 0
    while cursor < len(args) and args[cursor].value.startswith("-"):
        if args[cursor].value.startswith(_GIT_CONFIG_OVERRIDES):
            return False
        cursor += 2 if args[cursor].value in _GIT_VALUE_FLAGS else 1
    if cursor >= len(args) or args[cursor].value not in _GIT_READS:
        return False
    return not any(word.value.startswith("--output") for word in args[cursor + 1 :])
