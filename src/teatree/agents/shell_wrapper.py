"""Read the script a shell wrapper (``bash -lc '<script>'``) runs, without evaluating it.

Stdlib-only: the agent harnesses read commands with it before any Django setup.
"""

import shlex
from collections.abc import Iterator
from pathlib import PurePath

_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_WRAPPER_OPTIONS_WITH_VALUE = {
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "sudo": frozenset({"-u", "--user", "-g", "--group"}),
    "nohup": frozenset(),
    "command": frozenset(),
    "exec": frozenset(),
    "time": frozenset(),
}
_UV_RUN_OPTIONS_WITH_VALUE = frozenset(
    {"--with", "--directory", "--project", "--python", "-p", "--package", "--group", "--extra", "--env-file"}
)


def simple_commands(command: str) -> Iterator[list[str]]:
    """Each simple command's argv, split at shell operators; a line shlex cannot parse is split on whitespace."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        tokens = command.split()
    current: list[str] = []
    for token in tokens:
        if token and set(token) <= set("();<>|&"):
            if current:
                yield current
            current = []
        else:
            current.append(token)
    if current:
        yield current


def _skip_options(argv: list[str], with_value: frozenset[str]) -> list[str]:
    while argv and argv[0].startswith("-"):
        option = argv.pop(0)
        if option == "--":
            break
        if option in with_value and argv:
            argv.pop(0)
    return argv


def program_argv(argv: list[str]) -> list[str]:
    """*argv* with leading assignments and wrapper commands (``env``, ``timeout``, ``uv run``) removed."""
    argv = list(argv)
    while argv:
        if "=" in argv[0] and not argv[0].startswith("-"):
            argv.pop(0)
            continue
        name = PurePath(argv[0]).name
        if name in _WRAPPER_OPTIONS_WITH_VALUE:
            argv = _skip_options(argv[1:], _WRAPPER_OPTIONS_WITH_VALUE[name])
            if name == "timeout" and argv:
                argv.pop(0)
            continue
        if name == "uv" and argv[1:2] == ["run"]:
            argv = _skip_options(argv[2:], _UV_RUN_OPTIONS_WITH_VALUE)
            continue
        return argv
    return argv


def _shell_command_string(args: list[str]) -> str | None:
    """The script a shell runs from ``-c`` — also clustered, as codex's ``/bin/zsh -lc '<cmd>'``."""
    reads_string = False
    rest = list(args)
    while rest and rest[0][:1] in {"-", "+"} and rest[0] not in {"-", "--"}:
        option = rest.pop(0)
        if option[1:] in {"o", "O"} and rest:
            rest.pop(0)
        elif not option.startswith("--") and "c" in option[1:]:
            reads_string = True
    if rest[:1] == ["--"]:
        rest.pop(0)
    return rest[0] if reads_string and rest else None


def wrapper_script(argv: list[str]) -> str | None:
    """The ``-c`` script when *argv* is a shell invocation, else ``None``."""
    program = program_argv(argv)
    if program and PurePath(program[0]).name in _SHELLS:
        return _shell_command_string(program[1:])
    return None


def unwrap_lone_shell(command: str) -> str:
    """The script of a lone ``<shell> -c <script>`` command, else *command* unchanged."""
    argvs = list(simple_commands(command))
    script = wrapper_script(argvs[0]) if len(argvs) == 1 else None
    return command if script is None else script
