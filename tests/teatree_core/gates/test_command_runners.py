"""Runner and wrapper argv handling for the checkout placement scan."""

import pytest

from teatree.core.gates.command_runners import replace_strings, runner_argv, runner_directory


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        (["xargs", "-I", "%"], ("%",)),
        (["xargs", "-I%"], ("%",)),
        (["xargs", "-J", "@"], ("@",)),
        (["xargs", "-i"], ("{}",)),
        (["xargs", "-iR"], ("R",)),
        (["xargs", "--replace"], ("{}",)),
        (["xargs", "--replace=@"], ("@",)),
        (["parallel", "-I", "%%"], ("%%",)),
        (["xargs", "-n", "1"], ()),
        (["env", "-I", "%"], ()),
    ],
)
def test_replace_strings_come_only_from_xargs_or_parallel(prefix: list[str], expected: tuple[str, ...]) -> None:
    assert replace_strings(prefix) == expected


@pytest.mark.parametrize(
    ("runner", "args", "expected"),
    [
        ("sudo", ["-u", "root", "-D", "/srv"], "/srv"),
        ("sudo", ["-D/srv"], "/srv"),
        ("sudo", ["--chdir=/srv"], "/srv"),
        ("sudo", ["-D", "/a", "-D", "/b"], "/b"),
        ("parallel", ["--wd", "/srv"], "/srv"),
        ("parallel", ["--workdir=/srv"], "/srv"),
        ("sudo", ["env", "--chdir=/srv"], None),
        ("xargs", ["-D", "/srv"], None),
        ("sudo", ["-u", "root"], None),
    ],
)
def test_runner_directory_reads_only_the_runner_s_own_chdir_option(
    runner: str, args: list[str], expected: str | None
) -> None:
    assert runner_directory(runner, args) == expected


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        (["sudo", "--odd", "v", "git", "worktree"], ["git", "worktree"]),
        (["parallel", "-q", "git", "worktree"], ["git", "worktree"]),
        (["find", ".", "-exec", "bash", "-c", "x", ";"], ["bash", "-c", "x", ";"]),
        (["echo", "git", "worktree"], ["echo", "git", "worktree"]),
        (["xargs", "-n", "1", "git", "worktree"], ["git", "worktree"]),
    ],
)
def test_runner_argv_reaches_past_unmodelled_runner_options(words: list[str], expected: list[str]) -> None:
    assert runner_argv(words) == expected
