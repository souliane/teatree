"""Does a command text run pytest across the whole ``testpaths`` tree?

Extracted from ``tests/test_no_full_suite_on_pre_push.py`` (souliane/teatree#3994) so
the push-hook guard and the per-phase local-verification guard
(:mod:`teatree.quality.local_verification`) share ONE matcher instead of two copies
that drift apart — the trailing-slash blind spot the push guard closed would
otherwise have to be closed twice.

Tokenised per line via :mod:`shlex` (quotes collapse, ``#`` comments drop) so a command
boundary never lets one invocation absorb the next line's args, and linear in input
size — no backtracking regex, because this runs inside a git hook where a pathological
argument must not wedge the process.

The roots are passed in rather than discovered: ``testpaths`` lives in ``pyproject.toml``
and deriving it there keeps this module a pure stdlib leaf with no repo-root guessing.
"""

import shlex
import tomllib
from pathlib import Path, PurePosixPath

#: pytest/xdist options that never consume the next token, so a path following one is a
#: genuine collection target rather than a value. Every other option is assumed to take a
#: value: a plugin's options cannot be enumerated here, and over-consuming reports an
#: unscoped run while under-consuming hides one.
FLAG_OPTIONS: frozenset[str] = frozenset(
    {
        "-q",
        "-qq",
        "-v",
        "-vv",
        "-s",
        "-x",
        "-l",
        "--quiet",
        "--verbose",
        "--exitfirst",
        "--failed-first",
        "--last-failed",
        "--lf",
        "--ff",
        "--nf",
        "--no-header",
        "--collect-only",
        "--doctest-modules",
        "--strict-markers",
        "--strict-config",
        "--reuse-db",
        "--create-db",
        "--no-migrations",
        "--nomigrations",
        "--pdb",
    }
)

#: Shell tokens that end one command and begin the next.
COMMAND_SEPARATORS: frozenset[str] = frozenset({"&&", "||", ";", "|", "&"})


def declared_testpaths(pyproject: Path) -> tuple[str, ...]:
    """The declared ``testpaths`` from *pyproject*.

    Deriving the forbidden root from the config means a directory rename moves the root
    with it, instead of silently un-guarding the old name.
    """
    ini = tomllib.loads(pyproject.read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]
    return tuple(ini["testpaths"])


def _split_on_separators(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = [[]]
    for tok in tokens:
        if tok in COMMAND_SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(tok)
    return segments


def pytest_argvs(text: str) -> list[list[str]]:
    """The argv (tokens AFTER ``pytest``) of every pytest invocation in *text*."""
    argvs: list[list[str]] = []
    for line in text.splitlines():
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError:
            tokens = line.split()
        argvs.extend(seg[seg.index("pytest") + 1 :] for seg in _split_on_separators(tokens) if "pytest" in seg)
    return argvs


def positional_args(argv: list[str]) -> list[str]:
    """The positional (non-option, non-option-value) tokens of a pytest *argv*.

    An option outside :data:`FLAG_OPTIONS` is assumed to consume the next token — a plugin
    adds options this stdlib leaf cannot enumerate. Reading such a value as a collection
    target is what let ``pytest --basetemp /tmp/pt`` present as scoped while it collected
    the whole tree, so the ambiguous token is dropped and the invocation reads as unscoped.
    """
    positionals: list[str] = []
    consume_value = False
    for tok in argv:
        if consume_value:
            consume_value = False
            continue
        if tok.startswith("-"):
            consume_value = "=" not in tok and tok not in FLAG_OPTIONS
            continue
        positionals.append(tok)
    return positionals


def _is_testpaths_root(arg: str, roots: tuple[str, ...]) -> bool:
    candidate = PurePosixPath(arg)
    return any(candidate == PurePosixPath(root) for root in roots)


def runs_full_suite(command_text: str, roots: tuple[str, ...]) -> bool:
    """Whether *command_text* runs pytest across the whole *roots* tree.

    True for a bare ``pytest`` (no positional path) or one whose positional path
    normalises to a *roots* entry (``tests``, ``tests/``, ``./tests/``, quoted); a
    genuinely-scoped sub-path (``tests/quality``, ``tests/foo.py::T``) returns False.
    """
    for argv in pytest_argvs(command_text):
        positionals = positional_args(argv)
        if not positionals or any(_is_testpaths_root(p, roots) for p in positionals):
            return True
    return False
