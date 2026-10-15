"""The program a wrapped or runner-launched Bash command executes, and the context the runner gives it."""

import os
import shlex
from pathlib import Path

from teatree.core.gates.shell_payloads import SHELLS
from teatree.hooks.forge_subcommand import basename, program_words

_FLOCK_VALUE_OPTIONS = frozenset({"--timeout", "--wait", "--conflict-exit-code"})
_RUNNERS = frozenset({"xargs", "parallel", "sudo", "doas", "find"})
_SCANNED_PROGRAMS = frozenset({"git", "eval", *SHELLS})
_REPLACE_OPTIONS = ("-I", "-J", "-i")
_DIRECTORY_OPTIONS = {"sudo": frozenset({"-D", "--chdir"}), "parallel": frozenset({"--wd", "--workdir"})}
_WRAPPERS = frozenset(
    {"env", "timeout", "nice", "nohup", "stdbuf", "setsid", "time", "command", "exec", "flock", *_RUNNERS}
)


def runner_argv(words: list[str]) -> list[str]:
    """``program_words``, falling back past a runner's unmodelled options to the first git or shell word."""
    argv = program_words(words)
    tail = len(words) - len(argv)
    if not argv or basename(argv[0]) in _SCANNED_PROGRAMS:
        return argv
    if not any(basename(word) in _RUNNERS for word in words[: tail + 1]):
        return argv
    return next(
        (words[index:] for index in range(tail, len(words)) if basename(words[index]) in _SCANNED_PROGRAMS), argv
    )


def replace_strings(prefix: list[str]) -> tuple[str, ...]:
    """The strings an ``xargs``/``parallel`` in *prefix* substitutes into the command's own words."""
    found: list[str] = []
    runner = False
    for index, word in enumerate(prefix):
        if basename(word) in {"xargs", "parallel"}:
            runner = True
        elif runner and word in _REPLACE_OPTIONS[:2] and index + 1 < len(prefix):
            found.append(prefix[index + 1])
        elif runner and word.startswith(_REPLACE_OPTIONS):
            found.append(word[2:] or "{}")
        elif runner and word.startswith("--replace"):
            found.append(word.partition("=")[2] or "{}")
    return tuple(found)


def runner_directory(runner: str, args: list[str]) -> str | None:
    """The last working directory a ``sudo -D`` or ``parallel --wd`` in *args* runs the command in."""
    options = _DIRECTORY_OPTIONS.get(runner, frozenset())
    found: str | None = None
    for index, word in enumerate(args if options else ()):
        if basename(word) in _WRAPPERS:
            break
        name, _, glued = word.partition("=") if word.startswith("--") else (word[:2], "", word[2:])
        if name in options:
            found = glued or (args[index + 1] if index + 1 < len(args) else None)
    return found


def flock_command(argv: list[str]) -> list[str]:
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


def mktemp_directory(word: str) -> str | None:
    if not word.startswith("$(mktemp ") or not word.endswith(")"):
        return None
    try:
        args = shlex.split(word[2:-1])
    except ValueError:
        return None
    if args[:2] != ["mktemp", "-d"]:
        return None
    return _mktemp_template(args[2:])


def _mktemp_template(args: list[str]) -> str | None:
    directory: str | None = None
    template: str | None = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "-p":
            index += 1
            directory = args[index] if index < len(args) else None
        elif arg.startswith("--tmpdir="):
            directory = arg.partition("=")[2] or None
        elif arg in {"-t", "--tmpdir"}:
            directory = os.environ.get("TMPDIR")
            if directory is None:
                return None
        elif not arg.startswith("-"):
            template = arg
        index += 1
    if directory is not None:
        return str(Path(directory) / template) if template else directory
    return template


__all__ = ["flock_command", "mktemp_directory", "replace_strings", "runner_argv", "runner_directory"]
