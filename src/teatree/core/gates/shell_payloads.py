"""Script text a Bash command hands to a nested shell — heredocs, here-strings and piped echoes."""

import re

from teatree.hooks._shell_lexer import Token, TokenKind, tokenize
from teatree.hooks.forge_subcommand import basename

SHELLS = frozenset({"sh", "bash", "zsh"})
_HEREDOC_HEAD = re.compile(r"(?<!<)<<(?!<)(-)?[ \t]*(['\"]?)([A-Za-z_]\w*)\2")


def heredoc_scripts(command: str) -> list[str]:
    """Every heredoc body opened on a line that names a shell, which may read it as its script."""
    scripts: list[str] = []
    start = 0
    while (newline := command.find("\n", start)) >= 0:
        line = command[start:newline]
        start = newline + 1
        runs_shell = any(basename(token.value) in SHELLS for token in tokenize(line) if token.kind is TokenKind.WORD)
        for head in _HEREDOC_HEAD.finditer(line):
            indent = r"\t*" if head[1] else ""
            end = re.compile(rf"(?m)^{indent}{re.escape(head[3])}$").search(command, start)
            if runs_shell:
                scripts.append(command[start : end.start() if end else len(command)])
            start = end.end() + 1 if end else len(command)
    return scripts


def here_strings(words: list[str], raws: list[str]) -> list[str]:
    return [
        words[index + 1] if raw == "<<<" else words[index][3:]
        for index, raw in enumerate(raws)
        if raw.startswith("<<<") and (raw != "<<<" or index + 1 < len(words))
    ]


def piped_shell_payloads(tokens: list[Token]) -> list[str]:
    payloads: list[str] = []
    for index, token in enumerate(tokens):
        if token.kind is not TokenKind.OP or token.value != "|":
            continue
        start = index - 1
        while start >= 0 and tokens[start].kind is TokenKind.WORD:
            start -= 1
        left = [part.value for part in tokens[start + 1 : index]]
        right = tokens[index + 1 :]
        if not left[1:] or not right or right[0].kind is not TokenKind.WORD:
            continue
        if basename(left[0]) not in {"echo", "printf"} or basename(right[0].value) not in SHELLS:
            continue
        payload = left[1:]
        if left[0] == "printf" and payload[0].startswith("%"):
            payload = payload[1:]
        payloads.append(" ".join(payload))
    return payloads


__all__ = ["SHELLS", "here_strings", "heredoc_scripts", "piped_shell_payloads"]
