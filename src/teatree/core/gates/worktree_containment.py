"""Identify checkout destinations in executable Bash command segments."""

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from teatree.hooks._commit_repo_dir import segment_cwds
from teatree.hooks._shell_lexer import TokenKind, split_commands, tokenize
from teatree.hooks.forge_subcommand import basename, command_substitution_bodies, program_words, strip_heredoc_bodies

_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z_0-9]*)=(.*)$")
_VARIABLE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z_0-9]*)\}|([A-Za-z_][A-Za-z_0-9]*))")
_GIT_VALUE_OPTIONS = frozenset({"-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--exec-path"})
_ADD_VALUE_OPTIONS = frozenset({"-b", "-B", "--orphan", "--reason"})
_MOVE_POSITIONALS = 2
_MAX_NESTING = 3


@dataclass(frozen=True, slots=True)
class WorktreeCheckout:
    source: Path | None
    target: Path | None
    raw_target: str
    verb: str


def _path(word: str, base: Path | None) -> Path | None:
    candidate = Path(word).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (base / candidate).resolve() if base is not None else None


def _git_call(words: list[str], cwd: Path | None) -> tuple[str, list[str], Path | None] | None:
    if not words or basename(words[0]) != "git":
        return None
    source = cwd
    i = 1
    while i < len(words):
        word = words[i]
        if word == "-C" or (word.startswith("-C") and len(word) > len("-C")):
            value = words[i + 1] if word == "-C" and i + 1 < len(words) else word[2:]
            source = _path(value, source) if "$" not in value else None
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
            return word, words[i + 1 :], source
    return None


def _positional(args: list[str], *, add: bool) -> str | None:
    values: list[str] = []
    i = 0
    options = True
    while i < len(args):
        word = args[i]
        if word == "--":
            options = False
        elif options and word in (_ADD_VALUE_OPTIONS if add else frozenset()):
            i += 1
        elif options and word.startswith("-"):
            pass
        else:
            values.append(word)
        i += 1
    if add:
        return values[0] if values else None
    return values[-1] if len(values) >= _MOVE_POSITIONALS else None


def _flock_program(words: list[str]) -> list[str]:
    if not words or basename(words[0]) != "flock":
        return words
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
    expanded = [word]
    for match in matches:
        values = variables.get(match[1] or match[2])
        if not values:
            return (None,)
        expanded = [candidate.replace(match[0], value) for candidate in expanded for value in values]
    return tuple(expanded)


def _segment_checkouts(
    words: list[str], cwd: Path | None, variables: dict[str, tuple[str, ...]], depth: int
) -> list[WorktreeCheckout]:
    if words[:1] == ["for"] and words[2:3] == ["in"]:
        variables[words[1]] = tuple(words[3:])
    argv = _flock_program(program_words(words))
    if argv and basename(argv[0]) in {"sh", "bash", "zsh"}:
        if "-c" not in argv:
            return []
        index = argv.index("-c") + 1
        inner = argv[index] if index < len(argv) else ""
        return [WorktreeCheckout(cwd, None, inner, "shell")] if "$" in inner else _scan(inner, cwd, depth + 1)
    parsed = _git_call(argv, cwd)
    if parsed is None or parsed[0] != "worktree" or not parsed[1]:
        return []
    verb, args = parsed[1][0], parsed[1][1:]
    if verb not in {"add", "move"}:
        return []
    raw = _positional(args, add=verb == "add")
    if raw is None:
        return []
    return [
        WorktreeCheckout(parsed[2], _path(value, parsed[2]) if value else None, raw, verb)
        for value in _targets(raw, variables)
    ]


def _scan(command: str, cwd: Path | None, depth: int) -> list[WorktreeCheckout]:
    if depth > _MAX_NESTING:
        return [WorktreeCheckout(cwd, None, command, "shell")]
    cleaned = strip_heredoc_bodies(command)
    cwds = iter(segment_cwds(cleaned, cwd))
    variables: dict[str, tuple[str, ...]] = {}
    calls: list[WorktreeCheckout] = []
    for segment in split_commands(tokenize(cleaned)):
        words = [token.value for token in segment if token.kind is TokenKind.WORD]
        if not words:
            continue
        while words and (assignment := _ASSIGNMENT.fullmatch(words[0])) is not None:
            variables[assignment[1]] = (assignment[2],)
            words = words[1:]
        if words:
            calls.extend(_segment_checkouts(words, next(cwds, None), variables, depth))
    for inner in command_substitution_bodies(command):
        calls.extend(_scan(inner, cwd, depth + 1))
    return calls


def scan_worktree_checkouts(command: str, cwd: Path | None) -> list[WorktreeCheckout]:
    return _scan(command, cwd, 0)


__all__ = ["WorktreeCheckout", "scan_worktree_checkouts"]
