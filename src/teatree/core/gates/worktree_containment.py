"""Identify checkout destinations in executable Bash command segments.

The gate denies when ANY reported call is bad, so where bash's behaviour is not statically
knowable the scan reports every candidate (an unknown value as ``None``) rather than guess one.
"""

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from teatree.hooks._shell_lexer import Token, TokenKind, is_command_separator, tokenize
from teatree.hooks.forge_subcommand import basename, command_substitution_bodies, program_words, strip_heredoc_bodies
from teatree.utils.git_worktree import worktree_destination_index

_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z_0-9]*)(\+?)=(.*)$", re.DOTALL)
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*$")
_VARIABLE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z_0-9]*)\}|([A-Za-z_][A-Za-z_0-9]*))")
_GIT_VALUE_OPTIONS = frozenset({"-c", "--work-tree", "--namespace", "--config-env", "--exec-path"})
_FLOCK_VALUE_OPTIONS = frozenset({"--timeout", "--wait", "--conflict-exit-code"})
_DECLARATIONS = frozenset({"export", "readonly", "declare", "typeset", "local"})
_PLAIN_DECLARATION_FLAGS = frozenset("xrgp")
_REBINDERS = frozenset({"read", "unset", "mapfile", "readarray", "getopts", "printf"})
_LEADING_KEYWORDS = frozenset({"!", "{", "if", "then", "else", "elif", "do", "while", "until"})
_NAVIGATION = frozenset({"cd", "pushd", "popd"})
_SHELLS = frozenset({"sh", "bash", "zsh"})
_NAVIGATION_WITH_PATH = 2
_END = Token("", TokenKind.OP)
_MAX_NESTING = 3

type Values = tuple[str | None, ...]


@dataclass(frozen=True, slots=True)
class WorktreeCheckout:
    source: Path | None
    target: Path | None
    raw_target: str
    verb: str


@dataclass(slots=True)
class _Frame:
    cwds: tuple[Path | None, ...]
    variables: dict[str, Values]

    def copy(self) -> "_Frame":
        return _Frame(self.cwds, dict(self.variables))


def _home(word: str) -> str | None:
    try:
        return str(Path(word).expanduser())
    except RuntimeError:
        return None


def _path(word: str, base: Path | None) -> Path | None:
    candidate = Path(word)
    if candidate.is_absolute():
        return candidate.resolve()
    return (base / candidate).resolve() if base is not None else None


def _dir(word: str | None, base: Path | None) -> Path | None:
    expanded = _home(word) if word is not None and "$" not in word else None
    return _path(expanded, base) if expanded is not None else None


def _repo_of(git_dir: str | None, base: Path | None) -> Path | None:
    selected = _dir(git_dir, base)
    return selected.parent if selected is not None and selected.name == ".git" else selected


def _unique[T](items: list[T] | tuple[T, ...]) -> tuple[T, ...]:
    return tuple(dict.fromkeys(items))


def _aligned(words: list[str], source: list[str], raws: list[str]) -> list[str]:
    tail = len(source) - len(words)
    return raws[tail:] if tail >= 0 and source[tail:] == words else list(words)


def _unquoted_parens(raw: str) -> str:
    parens: list[str] = []
    quote = ""
    index = 0
    while index < len(raw):
        char = raw[index]
        if quote:
            if char == "\\" and quote == '"':
                index += 1
            elif char == quote:
                quote = ""
        elif char == "\\":
            index += 1
        elif char in "'\"":
            quote = char
        elif char in "()":
            parens.append(char)
        index += 1
    return "".join(parens)


def _flock_command(argv: list[str]) -> list[str]:
    index = 1
    while index < len(argv) and argv[index].startswith("-") and argv[index] != "-":
        option = argv[index]
        index += 1
        if option == "--":
            break
        if option in _FLOCK_VALUE_OPTIONS or (not option.startswith("--") and option[-1] in "wE"):
            index += 1
    command = argv[index + 1 :]
    if command[:1] in (["-c"], ["--command"]):
        return ["sh", "-c", command[1]] if len(command) > 1 else []
    return command


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


def _piped_shell_payloads(tokens: list[Token]) -> list[str]:
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
        if basename(left[0]) not in {"echo", "printf"} or basename(right[0].value) not in _SHELLS:
            continue
        payload = left[1:]
        if left[0] == "printf" and payload[0].startswith("%"):
            payload = payload[1:]
        payloads.append(" ".join(payload))
    return payloads


class _ShellScan:
    def __init__(self, cwds: tuple[Path | None, ...], depth: int, variables: dict[str, Values] | None = None) -> None:
        self.depth = depth
        self.frames = [_Frame(cwds, dict(variables or {}))]
        self.seen: dict[Path | None, None] = dict.fromkeys(cwds)
        self.git_dirs: dict[str | None, None] = dict.fromkeys(self.frames[0].variables.get("GIT_DIR", ()))
        self.tainted: set[str] = set()
        self.calls: list[WorktreeCheckout] = []

    def scan(self, command: str) -> list[WorktreeCheckout]:
        if self.depth > _MAX_NESTING:
            if "worktree" not in command:
                return []
            return [WorktreeCheckout(cwd, None, command, "shell") for cwd in self.frames[0].cwds]
        tokens = tokenize(strip_heredoc_bodies(command))
        self._walk(tokens)
        inherited = {"GIT_DIR": tuple(self.git_dirs)} if self.git_dirs else {}
        for inner in [*_piped_shell_payloads(tokens), *command_substitution_bodies(command)]:
            self.calls.extend(_ShellScan(tuple(self.seen), self.depth + 1, inherited).scan(inner))
        return self.calls

    def _walk(self, tokens: list[Token]) -> None:
        segment: list[Token] = []
        before = ""
        for token in [*tokens, _END]:
            if token.kind is TokenKind.WORD:
                segment.append(token)
                continue
            if token is not _END and not is_command_separator(token):
                continue
            if segment:
                self._segment(segment, isolated="|" in {before, token.value} or token.value == "&")
                segment = []
            if token.value == "(":
                self._push()
            before = token.value

    def _segment(self, tokens: list[Token], *, isolated: bool) -> None:
        words, raws, closes, opens = self._logical_words(tokens)
        if isolated:
            self._push()
        self._command(words, raws)
        if isolated:
            self._pop()
        for _ in range(closes):
            self._pop()
        for _ in range(opens):
            self._push()

    def _logical_words(self, tokens: list[Token]) -> tuple[list[str], list[str], int, int]:
        words: list[str] = []
        raws: list[str] = []
        depth = closes = 0
        joining = False
        for token in tokens:
            value = token.value
            unmatched = 0
            for paren in _unquoted_parens(token.raw):
                if paren == "(":
                    depth += 1
                elif depth:
                    depth -= 1
                elif closes < len(self.frames) - 1:
                    closes += 1
                    unmatched += 1
            if unmatched and value.endswith(")" * unmatched):
                value = value[:-unmatched]
            if joining:
                words[-1] += " " + value
                raws[-1] += " " + token.raw
            else:
                words.append(value)
                raws.append(token.raw)
            joining = depth > 0
        return words, raws, closes, depth

    def _push(self) -> None:
        self.frames.append(self.frames[-1].copy())

    def _pop(self) -> None:
        if len(self.frames) > 1:
            self.frames.pop()

    def _command(self, words: list[str], raws: list[str]) -> None:
        start = 0
        while start < len(words) and words[start] in _LEADING_KEYWORDS:
            start += 1
        assignments: list[tuple[re.Match[str], str]] = []
        while start < len(words) and (match := _ASSIGNMENT.fullmatch(words[start])) is not None:
            assignments.append((match, raws[start]))
            start += 1
        words, raws = words[start:], raws[start:]
        self.seen.update(dict.fromkeys(self.frames[-1].cwds))
        if not words:
            for match, raw in assignments:
                self._assign(match[1], self._value(match, raw), append=bool(match[2]))
            return
        env = {match[1]: (None,) if match[2] else self._value(match, raw) for match, raw in assignments}
        if basename(words[0]) in _DECLARATIONS:
            self._declare(words, raws)
        elif words[0] in _NAVIGATION:
            self._navigate(words, raws)
        elif words[0] == "for" and len(words) > 1 and _IDENTIFIER.fullmatch(words[1]):
            self._bind_loop(words, raws)
        else:
            self._taint(words)
            self._execute(words, raws, env)

    def _bind_loop(self, words: list[str], raws: list[str]) -> None:
        if words[2:3] != ["in"]:
            self._assign(words[1], (None,))
            return
        items = zip(words[3:], raws[3:], strict=True)
        self._assign(
            words[1], _unique([v for word, raw in items for v in self._expand(word, tilde=raw.startswith("~"))])
        )

    def _value(self, match: re.Match[str], raw: str) -> Values:
        return self._expand(match[3], tilde=raw.partition("=")[2].startswith("~"))

    def _assign(self, name: str, values: Values, *, append: bool = False) -> None:
        variables = self.frames[-1].variables
        if append:
            values = _unique(
                [
                    a + b if a is not None and b is not None else None
                    for a in variables.get(name, (None,))
                    for b in values
                ]
            )
        variables[name] = values or (None,)
        if name == "GIT_DIR":
            self.git_dirs.update(dict.fromkeys(variables[name]))

    def _declare(self, words: list[str], raws: list[str]) -> None:
        flags = {flag for word in words[1:] if word[:1] in "-+" for flag in word[1:]}
        certain = flags <= _PLAIN_DECLARATION_FLAGS
        for word, raw in zip(words[1:], raws[1:], strict=True):
            match = _ASSIGNMENT.fullmatch(word)
            if match is not None and certain:
                self._assign(match[1], self._value(match, raw), append=bool(match[2]))
            elif match is not None or (_IDENTIFIER.fullmatch(word) and (not certain or basename(words[0]) == "local")):
                self.tainted.add(match[1] if match is not None else word)

    def _taint(self, words: list[str]) -> None:
        rebinder = basename(words[0]) in _REBINDERS
        for word in words:
            if (match := _ASSIGNMENT.fullmatch(word)) is not None:
                self.tainted.add(match[1])
            elif rebinder and _IDENTIFIER.fullmatch(word):
                self.tainted.add(word)

    def _navigate(self, words: list[str], raws: list[str]) -> None:
        frame = self.frames[-1]
        if words[0] == "popd" or len(words) != _NAVIGATION_WITH_PATH:
            frame.cwds = (None,)
            return
        values = self._expand(words[1], tilde=raws[1].startswith("~"))
        landed = [_path(value, cwd) if value is not None else None for cwd in frame.cwds for value in values]
        frame.cwds = _unique([path if path is not None and path.is_dir() else None for path in landed])

    def _expand(self, raw_word: str, *, tilde: bool = False) -> Values:
        word = _home(raw_word) if tilde else raw_word
        if word is None:
            return (None,)
        if "$" not in word and "`" not in word:
            return (word,)
        if word.startswith("$(mktemp "):
            return (_mktemp_directory(word),)
        matches = list(_VARIABLE.finditer(word))
        if not matches or "$" in _VARIABLE.sub("", word) or "`" in word:
            return (None,)
        variables = self.frames[-1].variables
        expanded: list[str | None] = [""]
        offset = 0
        for match in matches:
            name = match[1] or match[2]
            values = (*variables.get(name, (None,)), *((None,) if name in self.tainted else ()))
            literal = word[offset : match.start()]
            expanded = [p + literal + v if p is not None and v is not None else None for p in expanded for v in values]
            offset = match.end()
        return _unique([prefix + word[offset:] if prefix is not None else None for prefix in expanded])

    def _execute(self, words: list[str], raws: list[str], env: dict[str, Values]) -> None:
        cwds = self.frames[-1].cwds
        while True:
            argv = program_words(words)
            tail = len(words) - len(argv)
            for index, word in enumerate(words[:tail] if words[tail:] == argv else words):
                if basename(word) == "env":
                    cwds, env = self._env(words[index + 1 :], cwds, env)
            raws = _aligned(argv, words, raws)
            if not argv or basename(argv[0]) != "flock":
                break
            words = _flock_command(argv)
            raws = _aligned(words, argv, raws)
        if argv and (basename(argv[0]) in _SHELLS or argv[0] == "eval"):
            self._script(argv, cwds, env)
        elif argv and basename(argv[0]) == "git":
            self._git(argv, raws, cwds, env)

    def _env(
        self, args: list[str], cwds: tuple[Path | None, ...], env: dict[str, Values]
    ) -> tuple[tuple[Path | None, ...], dict[str, Values]]:
        index = 0
        while index < len(args):
            word = args[index]
            if word in {"-C", "--chdir"}:
                index += 1
                cwds = _unique([_dir(args[index] if index < len(args) else "", cwd) for cwd in cwds])
            elif word.startswith(("-C", "--chdir=")):
                value = word.removeprefix("--chdir=") if word.startswith("--") else word[2:]
                cwds = _unique([_dir(value, cwd) for cwd in cwds])
            elif word in {"-u", "--unset", "-S", "--split-string"}:
                index += 1
            elif word == "--":
                break
            elif (match := _ASSIGNMENT.fullmatch(word)) is not None:
                env = {**env, match[1]: self._expand(match[3])}
            elif not word.startswith("-"):
                break
            index += 1
        return cwds, env

    def _script(self, argv: list[str], cwds: tuple[Path | None, ...], env: dict[str, Values]) -> None:
        if argv[0] == "eval":
            inner = " ".join(argv[1:])
        else:
            option = next((w for w in argv[1:] if w.startswith("-") and not w.startswith("--") and "c" in w[1:]), "")
            if not option:
                return
            index = argv.index(option) + 1
            inner = argv[index] if index < len(argv) else ""
        if re.fullmatch(r"\$\{?\w+\}?", inner):
            self.calls.extend(WorktreeCheckout(cwd, None, inner, "shell") for cwd in cwds)
            return
        git_dirs = env.get("GIT_DIR") or self.frames[-1].variables.get("GIT_DIR")
        self.calls.extend(_ShellScan(cwds, self.depth + 1, {"GIT_DIR": git_dirs} if git_dirs else None).scan(inner))

    def _git(self, argv: list[str], raws: list[str], cwds: tuple[Path | None, ...], env: dict[str, Values]) -> None:
        run_dirs = cwds
        git_dir: str | None = None
        index = 1
        while index < len(argv) and argv[index].startswith("-"):
            word = argv[index]
            if word.startswith("-C"):
                value = word[2:] if word != "-C" else (argv[index + 1] if index + 1 < len(argv) else "")
                run_dirs = _unique([_dir(value, run_dir) for run_dir in run_dirs])
                index += 1 if word != "-C" else 2
            elif word == "--git-dir" or word.startswith("--git-dir="):
                git_dir = (
                    word.partition("=")[2] if "=" in word else (argv[index + 1] if index + 1 < len(argv) else None)
                )
                index += 1 if "=" in word else 2
            else:
                index += 2 if word in _GIT_VALUE_OPTIONS else 1
        if argv[index : index + 1] != ["worktree"] or argv[index + 1 : index + 2] not in (["add"], ["move"]):
            return
        verb, args, arg_raws = argv[index + 1], argv[index + 2 :], raws[index + 2 :]
        pairs = [(source, run_dir) for run_dir in run_dirs for source in self._sources(run_dir, git_dir, env)]
        position = worktree_destination_index(args, add=verb == "add")
        if position is None:
            self.calls.extend(WorktreeCheckout(source, None, "<missing target>", verb) for source, _ in pairs)
            return
        raw = args[position]
        values = (None,) if raw in {"{}", "{1}"} else self._expand(raw, tilde=arg_raws[position].startswith("~"))
        self.calls.extend(
            WorktreeCheckout(source, _path(value, run_dir) if value is not None else None, raw, verb)
            for value in values
            for source, run_dir in pairs
        )

    def _sources(self, run_dir: Path | None, git_dir: str | None, env: dict[str, Values]) -> tuple[Path | None, ...]:
        if git_dir is not None:
            return _unique([_repo_of(value, run_dir) for value in self._expand(git_dir)])
        if "GIT_DIR" in env:
            return _unique([_repo_of(value, run_dir) for value in env["GIT_DIR"]])
        shell = self.frames[-1].variables.get("GIT_DIR", ())
        return _unique([run_dir, *(_repo_of(value, run_dir) for value in shell)])


def scan_worktree_checkouts(command: str, cwd: Path | None) -> list[WorktreeCheckout]:
    return _ShellScan((cwd,), 0).scan(command)


__all__ = ["WorktreeCheckout", "scan_worktree_checkouts"]
