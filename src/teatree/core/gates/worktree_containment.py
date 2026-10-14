"""Identify checkout destinations in executable Bash command segments."""

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from teatree.hooks._commit_repo_dir import segment_cwds
from teatree.hooks._shell_lexer import Token, TokenKind, split_commands, tokenize
from teatree.hooks.forge_subcommand import basename, command_substitution_bodies, program_words, strip_heredoc_bodies
from teatree.utils.git_worktree import worktree_destination

_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z_0-9]*)=(.*)$")
_VARIABLE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z_0-9]*)\}|([A-Za-z_][A-Za-z_0-9]*))")
_GIT_VALUE_OPTIONS = frozenset({"-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--exec-path"})
_MAX_NESTING = 3


@dataclass(frozen=True, slots=True)
class WorktreeCheckout:
    source: Path | None
    target: Path | None
    raw_target: str
    verb: str


def _path(word: str, base: Path | None, *, expand_tilde: bool = True) -> Path | None:
    candidate = Path(word).expanduser() if expand_tilde else Path(word)
    if candidate.is_absolute():
        return candidate.resolve()
    return (base / candidate).resolve() if base is not None else None


def _git_call(words: list[str], cwd: Path | None) -> tuple[str, list[str], Path | None, Path | None] | None:
    if not words or basename(words[0]) != "git":
        return None
    source = cwd
    target_base = cwd
    i = 1
    while i < len(words):
        word = words[i]
        if word == "-C" or (word.startswith("-C") and len(word) > len("-C")):
            value = words[i + 1] if word == "-C" and i + 1 < len(words) else word[2:]
            source = _path(value, source) if "$" not in value else None
            target_base = source
            i += 2 if word == "-C" else 1
        elif word in _GIT_VALUE_OPTIONS:
            if word == "--git-dir" and i + 1 < len(words):
                selected = _path(words[i + 1], cwd)
                source = selected.parent if selected is not None and selected.name == ".git" else selected
            i += 2
        elif word.startswith("--git-dir="):
            selected = _path(word.partition("=")[2], cwd)
            source = selected.parent if selected is not None and selected.name == ".git" else selected
            i += 1
        elif word.startswith("-"):
            i += 1
        else:
            return word, words[i + 1 :], source, target_base
    return None


def _flock_program(words: list[str]) -> list[str]:
    if not words or basename(words[0]) != "flock":
        return words
    if "-c" in words:
        index = words.index("-c") + 1
        return ["sh", "-c", words[index]] if index < len(words) else []
    i = 1
    while i < len(words) and words[i].startswith("-"):
        i += 2 if words[i] in {"-w", "--timeout", "-E", "--conflict-exit-code"} else 1
    return program_words(words[i + 1 :]) if i < len(words) else []


def _mktemp_directory(word: str) -> str | None:
    if not word.startswith("$(mktemp ") or not word.endswith(")"):
        return None
    try:
        args = shlex.split(word[2:-1])
    except ValueError:
        return None
    if args[:2] != ["mktemp", "-d"]:
        return None
    if "-p" in args:
        index = args.index("-p") + 1
        return args[index] if index < len(args) else None
    return next((arg for arg in reversed(args[2:]) if not arg.startswith("-")), None)


def _targets(word: str, variables: dict[str, tuple[str, ...]]) -> tuple[str | None, ...]:
    if "$" not in word:
        return (None,) if word in {"{}", "{1}"} else (word,)
    if word.startswith("$(mktemp "):
        return (_mktemp_directory(word),)
    matches = list(_VARIABLE.finditer(word))
    if not matches or "$" in _VARIABLE.sub("", word):
        return (None,)
    expanded = [""]
    offset = 0
    for match in matches:
        values = variables.get(match[1] or match[2])
        if not values:
            return (None,)
        resolved: list[str] = []
        for value in values:
            concrete = _mktemp_directory(value) if value.startswith("$(mktemp ") else value
            if concrete is None or "$" in concrete:
                return (None,)
            resolved.append(concrete)
        expanded = [candidate + word[offset : match.start()] + value for candidate in expanded for value in resolved]
        offset = match.end()
    return tuple(candidate + word[offset:] for candidate in expanded)


def _script_checkouts(argv: list[str], cwd: Path | None, depth: int) -> list[WorktreeCheckout] | None:
    if argv and basename(argv[0]) in {"sh", "bash", "zsh"}:
        options = next(
            (word for word in argv[1:] if word.startswith("-") and not word.startswith("--") and "c" in word[1:]),
            "",
        )
        if not options:
            return []
        index = argv.index(options) + 1
        inner = argv[index] if index < len(argv) else ""
    elif argv[:1] == ["eval"]:
        inner = " ".join(argv[1:])
    else:
        return None
    if re.fullmatch(r"\$\{?\w+\}?", inner):
        return [WorktreeCheckout(cwd, None, inner, "shell")]
    return _scan(inner, cwd, depth + 1)


def _env_cwd(words: list[str], cwd: Path | None) -> Path | None:
    if words[:1] != ["env"]:
        return cwd
    index = 1
    while index < len(words):
        word = words[index]
        if word in {"-C", "--chdir"} and index + 1 < len(words):
            cwd = _path(words[index + 1], cwd)
            index += 2
        elif word.startswith("--chdir="):
            cwd = _path(word.partition("=")[2], cwd)
            index += 1
        elif word in {"-u", "--unset", "-S", "--split-string"}:
            index += 2
        elif word == "--":
            break
        elif word.startswith("-") or _ASSIGNMENT.fullmatch(word):
            index += 1
        else:
            break
    return cwd


def _segment_checkouts(
    words: list[str], cwd: Path | None, variables: dict[str, tuple[str, ...]], depth: int, raw_words: list[str]
) -> list[WorktreeCheckout]:
    if words[:1] == ["for"] and words[2:3] == ["in"]:
        variables[words[1]] = tuple(words[3:])
    cwd = _env_cwd(words, cwd)
    argv = _flock_program(program_words(words))
    if (nested := _script_checkouts(argv, cwd, depth)) is not None:
        return nested
    parsed = _git_call(argv, cwd)
    if parsed is None or parsed[0] != "worktree" or not parsed[1]:
        return []
    verb, args = parsed[1][0], parsed[1][1:]
    if verb not in {"add", "move"}:
        return []
    raw = worktree_destination(args, add=verb == "add")
    if raw is None:
        return [WorktreeCheckout(parsed[2], None, "<missing target>", verb)]
    quoted_tilde = raw.startswith("~") and any(word.startswith(("'~", '"~')) for word in raw_words)
    return [
        WorktreeCheckout(
            parsed[2], _path(value, parsed[3], expand_tilde=not quoted_tilde) if value else None, raw, verb
        )
        for value in _targets(raw, variables)
    ]


def _consume_assignments(words: list[str], variables: dict[str, tuple[str, ...]]) -> list[str]:
    while words and (assignment := _ASSIGNMENT.fullmatch(words[0])) is not None:
        value = assignment[2]
        consumed = 1
        if value.startswith("$(mktemp"):
            while not value.endswith(")") and consumed < len(words):
                value += " " + words[consumed]
                consumed += 1
        variables[assignment[1]] = (value,)
        words = words[consumed:]
    return words


def _piped_shell_checkouts(tokens: list[Token], cwd: Path | None, depth: int) -> list[WorktreeCheckout]:
    calls: list[WorktreeCheckout] = []
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
        if basename(left[0]) not in {"echo", "printf"} or basename(right[0].value) not in {"sh", "bash", "zsh"}:
            continue
        payload = left[1:]
        if left[0] == "printf" and payload[0].startswith("%"):
            payload = payload[1:]
        calls.extend(_scan(" ".join(payload), cwd, depth + 1))
    return calls


def _scan(command: str, cwd: Path | None, depth: int) -> list[WorktreeCheckout]:
    if depth > _MAX_NESTING:
        return [WorktreeCheckout(cwd, None, command, "shell")] if "worktree" in command else []
    cleaned = strip_heredoc_bodies(command)
    cwds = iter(segment_cwds(cleaned, cwd))
    variables: dict[str, tuple[str, ...]] = {}
    calls: list[WorktreeCheckout] = []
    tokens = tokenize(cleaned)
    for segment in split_commands(tokens):
        segment_words = [token.value for token in segment if token.kind is TokenKind.WORD]
        if not segment_words:
            continue
        words = _consume_assignments(segment_words.copy(), variables)
        if not words and all(_ASSIGNMENT.fullmatch(word) for word in segment_words):
            continue
        segment_cwd = next(cwds, None)
        if words:
            calls.extend(_segment_checkouts(words, segment_cwd, variables, depth, [token.raw for token in segment]))
    calls.extend(_piped_shell_checkouts(tokens, cwd, depth))
    for inner in command_substitution_bodies(command):
        calls.extend(_scan(inner, cwd, depth + 1))
    return calls


def scan_worktree_checkouts(command: str, cwd: Path | None) -> list[WorktreeCheckout]:
    return _scan(command, cwd, 0)


__all__ = ["WorktreeCheckout", "scan_worktree_checkouts"]
