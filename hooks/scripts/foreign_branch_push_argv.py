"""What ONE ``git push`` invocation actually writes, read git's own way.

A LEAF: the shell walk that finds each push lives in
:mod:`foreign_branch_push_read`, and this module answers only what a single
push's argv writes. It reaches a repository through :func:`push_work_dir` alone
and imports nothing from the ownership ladder.
"""

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

from hooks.scripts.foreign_branch_push_git import push_work_dir

# git's leading global options that consume the NEXT token as their value.
_GIT_VALUE_FLAGS: Final[frozenset[str]] = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
)
# push options that consume the NEXT token; `-u`/`--set-upstream` take none.
_PUSH_VALUE_FLAGS: Final[frozenset[str]] = frozenset(
    {"--repo", "-o", "--push-option", "--receive-pack", "--exec", "--recurse-submodules"}
)
_LONG_PUSH_VALUE_FLAGS: Final[tuple[str, ...]] = tuple(flag for flag in _PUSH_VALUE_FLAGS if flag.startswith("--"))
_FORCE_FLAGS: Final[frozenset[str]] = frozenset({"-f", "--force", "--force-with-lease", "--force-if-includes"})
# A delete destroys strictly more than a force: the branch and its reflog both go.
_DELETE_FLAGS: Final[frozenset[str]] = frozenset({"-d", "--delete"})
_DRY_RUN_FLAGS: Final[frozenset[str]] = frozenset({"-n", "--dry-run"})
# Flags that make git print and exit, so the push in whose argv they sit never runs.
_NON_WRITING_FLAGS: Final[frozenset[str]] = frozenset({"--help", "-h"})
_REPO_FLAG: Final[str] = "--repo"
_CONFIG_FLAG: Final[str] = "-c"
# Write every branch the local repo has, without naming one — no target to pin.
_UNNAMED_TARGET_FLAGS: Final[frozenset[str]] = frozenset({"--all", "--mirror", "--branches"})
# `-o`/`--push-option` is git push's only short option taking a required argument.
_SHORT_VALUE_LETTERS: Final[frozenset[str]] = frozenset("o")


@dataclass(frozen=True, slots=True)
class PushSpec:
    """One ``git push`` invocation: where it runs, what it writes, how hard."""

    work_dir: str
    remote: str
    refspecs: tuple[str, ...]
    force: bool
    destructive: bool
    unpinnable: str
    config_overrides: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _PushArgs:
    """One push's argv split the way git splits it: flags, their values, operands."""

    flags: tuple[tuple[str, str], ...]
    operands: tuple[str, ...]


def git_push_spec(leading: list[str], work_dir: str) -> PushSpec | None:
    """The WRITING push *leading* runs, or ``None`` when it is not a push or writes nothing.

    *leading* is one shell segment past its run-the-next-word wrappers; a push whose
    own arguments carry ``--dry-run``/``-n`` or ``--help`` writes nothing.
    """
    parsed = _git_push_args(leading)
    if parsed is None:
        return None
    args, overrides = parsed
    push_dir, unpinnable = push_work_dir(leading, work_dir)
    return _spec(args, push_dir, overrides, repo_unpinnable=unpinnable)


def _git_push_args(tokens: list[str]) -> tuple[list[str], tuple[str, ...]] | None:
    """A ``git push``'s own arguments and the ``-c`` pairs preceding it, else ``None``.

    The pairs travel because they decide what a refspec-less push writes; git rejects
    the glued `-cK=V` form (exit 129), so the separated one is the only shape to read.
    """
    if PurePosixPath(tokens[0]).name != "git":
        return None
    cursor = 1
    overrides: list[str] = []
    while cursor < len(tokens) and tokens[cursor].startswith("-"):
        if tokens[cursor] == _CONFIG_FLAG and cursor + 1 < len(tokens):
            overrides.append(tokens[cursor + 1])
        cursor += 2 if tokens[cursor] in _GIT_VALUE_FLAGS else 1
    if cursor >= len(tokens) or tokens[cursor] != "push":
        return None
    return tokens[cursor + 1 :], tuple(overrides)


def _spec(args: list[str], work_dir: str, overrides: tuple[str, ...], *, repo_unpinnable: str = "") -> PushSpec | None:
    """The :class:`PushSpec` for one push's *args*, or ``None`` when it writes nothing."""
    walked = _walk(args)
    if any(flag in _DRY_RUN_FLAGS or flag in _NON_WRITING_FLAGS for flag, _ in walked.flags):
        return None
    refspecs = walked.operands[1:]
    force = _abbreviates(walked, _FORCE_FLAGS) or any(spec.startswith("+") for spec in refspecs)
    delete = _abbreviates(walked, _DELETE_FLAGS) or any(spec.removeprefix("+").startswith(":") for spec in refspecs)
    unnamed = sorted(flag for flag, _ in walked.flags if _matches(flag, _UNNAMED_TARGET_FLAGS))
    repo = next((value for flag, value in walked.flags if flag == _REPO_FLAG), "")
    return PushSpec(
        work_dir=work_dir,
        remote=walked.operands[0] if walked.operands else (repo or "origin"),
        refspecs=refspecs,
        force=force,
        destructive=force or delete,
        unpinnable=repo_unpinnable or (f"`git push {unnamed[0]}` names no branch, so its targets" if unnamed else ""),
        config_overrides=overrides,
    )


def _walk(args: list[str]) -> _PushArgs:
    """Split *args* into flag positions and operand positions, git's own way.

    Every question below asks it rather than the raw argv, because a token at a
    VALUE position is neither: `git push -o -n origin br` LANDED the ref while a
    scan for `-n` read it as a dry run and yielded no spec at all. A short token is a
    CLUSTER, so `-oskip.notify` is `-o` carrying a value whose letters are not flags.
    """
    flags: list[tuple[str, str]] = []
    operands: list[str] = []
    end_of_flags = False
    skip = False
    for index, arg in enumerate(args):
        if skip:
            skip = False
        elif end_of_flags or not arg.startswith("-") or arg == "-":
            operands.append(arg)
        elif arg == "--":
            end_of_flags = True
        elif arg.startswith("--"):
            name, equals, glued = arg.partition("=")
            skip = not equals and _consumes_value(arg)
            flags.append((name, glued if equals else (args[index + 1] if skip and index + 1 < len(args) else "")))
        else:
            skip = _expand_cluster(arg, args[index + 1 :], flags)
    return _PushArgs(flags=tuple(flags), operands=tuple(operands))


def _expand_cluster(arg: str, rest: list[str], flags: list[tuple[str, str]]) -> bool:
    """Emit one flag per clustered letter; report whether the NEXT token became a value.

    Letters are consumed left to right, and on a value-taking one the remainder of the
    token is its value — the next token only when the token ends there. Measured under
    git 2.50.1: `-fovalue.a` force-pushed with push option `value.a`, and `-of` pushed
    with push option `f` and no force at all.
    """
    for position, letter in enumerate(arg[1:], start=1):
        if letter in _SHORT_VALUE_LETTERS:
            glued = arg[position + 1 :]
            flags.append((f"-{letter}", glued or (rest[0] if rest else "")))
            return not glued and bool(rest)
        flags.append((f"-{letter}", ""))
    return False


def _consumes_value(arg: str) -> bool:
    """Whether the token after this LONG option is its value rather than an operand.

    Short options never reach here: `_expand_cluster` has already given `-o` its value,
    and reading that value's letters as flags is what let `-oskip.notify` blind the gate.
    Prefix matching runs in the value-consuming direction ONLY, so `git push --dry origin
    main` reads as WRITING and may draw a fail-closed refusal on a push git never makes,
    while an ambiguous prefix git itself rejects (`--rec`, exit 129) never pushes.
    """
    return any(flag.startswith(arg) for flag in _LONG_PUSH_VALUE_FLAGS)


def _abbreviates(args: _PushArgs, flags: frozenset[str]) -> bool:
    """Whether a FLAG position carries one of *flags* or an abbreviation git expands to one."""
    return any(_matches(flag, flags) for flag, _ in args.flags)


def _matches(arg: str, flags: frozenset[str]) -> bool:
    """Whether *arg* is one of *flags*, or a prefix git would resolve to one.

    Prefix-matched are the sets whose membership makes this gate refuse MORE
    (`_FORCE_FLAGS`, `_DELETE_FLAGS`, `_UNNAMED_TARGET_FLAGS`, `_PUSH_VALUE_FLAGS`);
    exact-matched are the sets whose membership makes it refuse LESS (`_DRY_RUN_FLAGS`,
    `_NON_WRITING_FLAGS`), because reading `--d` as a dry run re-opens the very bypass
    this gate closes. One blanket rule for both directions is what made that a hole.

    Accepted over-refusal: `--f`/`--fo`/`--forc` are AMBIGUOUS to git (exit 129, no
    push) and read as force here — a spurious refusal on a command git never runs.
    """
    return any(flag.startswith(arg) for flag in flags)
