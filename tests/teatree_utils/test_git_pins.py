import pytest

from teatree.utils.git_pins import GitPins

FSMONITOR_OFF = ["-c", "core.fsmonitor="]
HOOKS_OFF = ["-c", "core.hooksPath=/dev/null"]


@pytest.mark.parametrize(
    ("cmd", "expected"),
    [
        (
            ["git", "-C", "r", "-c", "a=b", "status"],
            ["git", *FSMONITOR_OFF, *HOOKS_OFF, "-C", "r", "-c", "a=b", "status"],
        ),
        (
            ["git", "--no-optional-locks", "-C", "r", "fetch"],
            ["git", *FSMONITOR_OFF, *HOOKS_OFF, "--no-optional-locks", "-C", "r", "fetch"],
        ),
        (["git", "-C", "commit", "status"], ["git", *FSMONITOR_OFF, *HOOKS_OFF, "-C", "commit", "status"]),
        (["/usr/bin/git", "status"], ["/usr/bin/git", *FSMONITOR_OFF, *HOOKS_OFF, "status"]),
        (["git"], ["git", *FSMONITOR_OFF, *HOOKS_OFF]),
    ],
    ids=["value-options-skipped", "flag-options-skipped", "verb-name-as-option-value", "path-form-leader", "bare-git"],
)
def test_pins_both_settings_on_a_verb_that_fires_no_gate_hook(cmd: list[str], expected: list[str]) -> None:
    assert GitPins.argv(cmd) == expected


@pytest.mark.parametrize(
    "cmd",
    [
        ["git", "-C", "r", "commit", "-m", "x"],
        ["git", "--git-dir=x", "merge", "--ff-only", "origin/main"],
        ["git", "--git-dir", "x", "pull", "--ff-only"],
        ["git", "-C", "r", "push", "origin", "main"],
    ],
    ids=["commit", "merge", "pull", "push"],
)
def test_keeps_the_hooks_of_a_verb_that_fires_an_installed_gate_hook(cmd: list[str]) -> None:
    pinned = GitPins.argv(cmd, hooks_path="/trusted/hooks")

    assert pinned[:5] == ["git", *FSMONITOR_OFF, "-c", "core.hooksPath=/trusted/hooks"]
    assert pinned[5:] == cmd[1:]


def test_hook_bearing_verb_refuses_an_unknown_default_hooks_path() -> None:
    with pytest.raises(ValueError, match="default hooks path"):
        GitPins.argv(["git", "push", "origin", "main"])


def test_common_dir_probe_preserves_global_options_and_disables_executable_config() -> None:
    cmd = ["git", "-C", "r", "-c", "core.hooksPath=/planted", "push", "origin", "main"]

    assert GitPins.common_dir_probe(cmd) == [
        "git",
        "-C",
        "r",
        "-c",
        "core.hooksPath=/planted",
        *FSMONITOR_OFF,
        *HOOKS_OFF,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    ]


def test_common_dir_probe_requires_a_verb() -> None:
    with pytest.raises(ValueError, match="no verb"):
        GitPins.common_dir_probe(["git"])


@pytest.mark.parametrize(
    "cmd",
    [
        ["git", "-C", "r", "config", "--get", "core.hooksPath"],
        ["git", "-C", "r", "rev-parse", "--git-path", "hooks"],
    ],
    ids=["config", "rev-parse"],
)
def test_keeps_hooks_path_visible_to_the_verbs_that_read_it(cmd: list[str]) -> None:
    assert GitPins.argv(cmd) == ["git", *FSMONITOR_OFF, *cmd[1:]]


@pytest.mark.parametrize(
    "cmd", [["echo", "status"], ["git-lfs", "status"], ["uv", "run", "git"], []], ids=["echo", "git-lfs", "uv", "empty"]
)
def test_leaves_a_command_that_is_not_git_untouched(cmd: list[str]) -> None:
    assert GitPins.argv(cmd) == cmd


def test_returns_a_new_list_and_leaves_the_caller_argv_alone() -> None:
    cmd = ["git", "status"]

    pinned = GitPins.argv(cmd)

    assert cmd == ["git", "status"]
    assert pinned is not cmd
