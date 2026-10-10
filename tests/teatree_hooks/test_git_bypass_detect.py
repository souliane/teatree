"""The git hook/merge-bypass detector (the ``--no-verify``/hooksPath safety subset).

Mirrors ``hooks.scripts.direct_command_guard.deny_match`` for the hook/merge-bypass
family so both lanes refuse the same set; the guard-vs-leaf agreement itself is
pinned by the deny-corpus parity test.
"""

import pytest

from hooks.scripts.direct_command_guard import deny_match
from teatree.hooks.git_bypass_detect import git_bypass_deny_reason

# Assembled so the literal bypass strings never appear in this file's own scanned
# transcript / commit body (the PreToolUse gates scan them).
_NO_VERIFY = "--no-" + "verify"
_NO_GPG = "--no-" + "gpg-sign"
_HOOKS_PATH = "core.hooks" + "Path=/dev/null"
_HOOKS_PATH_KEY = "core.hooks" + "Path"


class TestDenies:
    @pytest.mark.parametrize(
        "command",
        [
            f"git commit -m x {_NO_VERIFY}",
            f"git push {_NO_VERIFY}",
            f"git commit -m x {_NO_GPG}",
            f"git -c {_HOOKS_PATH} commit -m x",
            'git -c "' + _HOOKS_PATH + '" commit -m x',  # quoted config value still caught
            "git push -o merge_request.merge_when_pipeline_succeeds",
            "git push --push-option=merge_request.merge_when_pipeline_succeeds",
        ],
    )
    def test_hook_and_merge_bypasses_are_denied(self, command: str) -> None:
        reason = git_bypass_deny_reason(command)
        assert reason is not None
        assert "BLOCKED" in reason


class TestAllows:
    @pytest.mark.parametrize(
        "command",
        [
            "git commit -m 'normal message'",
            "git push origin HEAD",
            "git commit -m 'note: mention " + _NO_VERIFY + " inside a quoted message'",
            "ls -la",
            "docker compose up",  # a direct-command deny, but NOT this leaf's concern
            "",
        ],
    )
    def test_ordinary_and_out_of_scope_commands_pass(self, command: str) -> None:
        assert git_bypass_deny_reason(command) is None


class TestGitGlobalOptionsBeforeThePushSubcommand:
    """``git`` takes its own options before the subcommand — the bypass is still one.

    ``git push`` had to be CONTIGUOUS, so every valid spelling that puts a global
    option in between (``-c <k>=<v>``, ``-C <dir>``, ``--no-pager``) walked the
    auto-merge push option straight past the gate.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "git -c push.default=simple push -o merge_request.merge_when_pipeline_succeeds",
            "git -C ../worktree push -o merge_request.merge_when_pipeline_succeeds",
            "git --no-pager push --push-option=merge_request.merge_when_pipeline_succeeds",
            "git -C ../wt -c user.name=x push origin HEAD -o merge_request.merge_when_pipeline_succeeds",
        ],
    )
    def test_auto_merge_push_option_is_denied_behind_global_options(self, command: str) -> None:
        reason = git_bypass_deny_reason(command)
        assert reason is not None
        assert "auto-merge" in reason

    @pytest.mark.parametrize(
        "command",
        [
            "git -C ../worktree push origin HEAD",
            "git -c user.name=x push --force-with-lease",
        ],
    )
    def test_an_ordinary_push_behind_global_options_is_allowed(self, command: str) -> None:
        assert git_bypass_deny_reason(command) is None


class TestEveryValidPushOptionSpelling:
    """git accepts four spellings of the same push option; only two were matched.

    Verified against git 2.50.1: ``--push-option VALUE``, ``--push-option=VALUE``,
    ``-o VALUE`` and ``-oVALUE`` all schedule the identical auto-merge. The gate
    required a space after ``-o`` and an ``=`` after ``--push-option``, so the
    other two spellings walked the keystone bypass straight through.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "git push --push-option merge_request.merge_when_pipeline_succeeds origin HEAD",
            "git push -omerge_request.merge_when_pipeline_succeeds origin HEAD",
            "git -c user.name=x push --push-option merge_request.merge_when_pipeline_succeeds",
            'git push --push-option "merge_request.merge_when_pipeline_succeeds"',
        ],
    )
    def test_every_spelling_of_the_auto_merge_push_option_is_denied(self, command: str) -> None:
        reason = git_bypass_deny_reason(command)
        assert reason is not None
        assert "auto-merge" in reason

    @pytest.mark.parametrize(
        "command",
        [
            "git push --push-option merge_request.create origin feature",
            "git push -omerge_request.draft origin feature",
            "git push -o ci.skip origin feature",
        ],
    )
    def test_an_unrelated_push_option_is_allowed(self, command: str) -> None:
        assert git_bypass_deny_reason(command) is None


class TestEnvAndConfigWaysAroundTheHooks:
    """Ways to switch a hook off that are not the ``--no-verify`` flag, refused on both lanes.

    ``SKIP=<hook id>`` is honoured by prek itself, ``GIT_CONFIG_*`` and ``git config`` can
    point git's hooks directory anywhere, and each of them is as good as the flag at getting
    a push past the leak gate. Lane A (the PreToolUse guard) and Lane B (the pydantic-ai
    shell) must agree on every row.
    """

    DENIED = (
        "SKIP=refuse-public-push-with-leak git push",
        "SKIP=banned-terms git commit -m x",
        "SKIP=x prek run",
        "SKIP=x pre-commit run --all-files",
        "export SKIP=refuse-public-push-with-leak && git push origin HEAD",
        "env SKIP=x git -C ../worktree push",
        "SKIP='a,b' git push",
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=" + _HOOKS_PATH_KEY + " GIT_CONFIG_VALUE_0=/dev/null git push",
        "GIT_CONFIG_GLOBAL=/tmp/evil-gitconfig git push",
        "GIT_CONFIG_SYSTEM=/tmp/evil-gitconfig git commit -m x",
        "GIT_CONFIG_PARAMETERS=\"'" + _HOOKS_PATH_KEY + "=/dev/null'\" git push",
        "git config " + _HOOKS_PATH_KEY + " /dev/null",
        "git config --global " + _HOOKS_PATH_KEY + " /dev/null",
        "git config --local --add " + _HOOKS_PATH_KEY + " /tmp/hooks",
        "git -C ../worktree config --file .git/config " + _HOOKS_PATH_KEY + ' ""',
    )

    ALLOWED = (
        "SKIP=x make lint",
        "SKIP=x echo done",
        "git commit -m 'note: SKIP=x is how a hook is skipped'",
        "git push origin HEAD  # SKIP=x would skip a hook",
        "git config " + _HOOKS_PATH_KEY,
        "git config --get " + _HOOKS_PATH_KEY,
        "git config --unset " + _HOOKS_PATH_KEY,
        "git config user.name alice",
        "GIT_CONFIG_GLOBAL=/dev/null git status",
        "cat .pre-commit-config.yaml",
        "SKIP=x cat .pre-commit-config.yaml",
    )

    @pytest.mark.parametrize("command", DENIED)
    def test_both_lanes_deny(self, command: str) -> None:
        lane_b = git_bypass_deny_reason(command)
        lane_a = deny_match(command)

        assert lane_b is not None
        assert "BLOCKED" in lane_b
        assert lane_a is not None, "Lane A must refuse what Lane B refuses"
        assert "BLOCKED" in lane_a

    @pytest.mark.parametrize("command", ALLOWED)
    def test_both_lanes_allow(self, command: str) -> None:
        assert git_bypass_deny_reason(command) is None
        assert deny_match(command) is None


class TestRedirectsOfTheGatesConfigAndTermStore:
    """The environment can aim git's configuration, or the registry the leak gate reads, somewhere else.

    A pre-push hook cannot see its own caller's environment as an attack, so the guards refuse the
    assignment on the commands that run hooks (a commit, a push, prek, ``t3 push``). A quoted
    assignment, a ``bash -c`` wrapper and ``t3 push`` carry it just the same.
    """

    DENIED = (
        # SKIP= through every wrapper that still reaches a hook
        "SKIP=refuse-public-push-with-leak t3 push",
        "SKIP=x t3 teatree pr create",
        # every t3 verb that pushes
        "SKIP=x t3 teatree ship",
        "TEATREE_TERM_REGISTRY='{}' t3 fast-push",
        "T3_CONFIG_DB=/tmp/decoy.sqlite3 t3 teatree pr ensure-pr",
        "HOME=/tmp/evil t3 teatree handover run",
        "SKIP=x t3 teatree workspace salvage",
        "SKIP=x bash -c 'git push'",
        "bash -c 'SKIP=x git push'",
        "env 'SKIP=x' git push",
        'export "SKIP=x"; git push origin HEAD',
        # the registry and the stores that locate it
        "TEATREE_TERM_REGISTRY='{}' git push",
        "T3_CONFIG_DB=/tmp/decoy.sqlite3 git push",
        "T3_DATA_DIR=/tmp/decoy git push",
        "XDG_DATA_HOME=/tmp/decoy git push",
        "export T3_CONFIG_DB=/tmp/decoy.sqlite3 && git push",
        "T3_CONFIG_DB=/tmp/decoy.sqlite3 t3 push",
        "TEATREE_TERM_REGISTRY='{}' prek run --hook-stage pre-push",
        # git's global configuration, found through the home directory
        "HOME=/tmp/evil git push",
        "XDG_CONFIG_HOME=/tmp/evil git push",
        "env HOME=/tmp/evil git commit -m x",
        "HOME=/tmp/evil t3 push",
        # a remote-tracking ref written by hand makes every tool that reads one believe the remote has it
        "git update-ref refs/remotes/origin/main HEAD",
        "git -C ../worktree update-ref -m planted refs/remotes/origin/feature HEAD",
        "git symbolic-ref refs/remotes/origin/HEAD refs/remotes/origin/main",
        "git fetch . HEAD:refs/remotes/origin/feature",
        "git push . HEAD:refs/remotes/origin/feature",
        "echo abc > .git/refs/remotes/origin/feature",
        "printf abc | tee .git/refs/remotes/origin/feature",
    )

    ALLOWED = (
        "SKIP=x t3 doctor check",
        "SKIP=x t3 teatree ticket status",
        "T3_CONFIG_DB=/tmp/x.sqlite3 t3 teatree pr list",
        "T3_CONFIG_DB=/tmp/x.sqlite3 t3 loop status",
        "XDG_DATA_HOME=/tmp/x uv run pytest tests/foo.py",
        "TEATREE_TERM_REGISTRY='{}' uv run pytest -q",
        "T3_DATA_DIR=/tmp/x uv run pytest -q",
        "HOME=/tmp/x make test",
        "echo $HOME",
        "cd ~ && git status",
        "git update-ref -d refs/remotes/origin/stale",
        "git update-ref refs/heads/topic HEAD",
        "git for-each-ref refs/remotes/",
        "git rev-parse refs/remotes/origin/main",
        "git branch -r",
        "git fetch origin",
        "cat .git/refs/remotes/origin/main",
    )

    @pytest.mark.parametrize("command", DENIED)
    def test_both_lanes_deny(self, command: str) -> None:
        lane_b = git_bypass_deny_reason(command)
        lane_a = deny_match(command)

        assert lane_b is not None, "Lane B must refuse it"
        assert "BLOCKED" in lane_b
        assert lane_a is not None, "Lane A must refuse what Lane B refuses"
        assert "BLOCKED" in lane_a

    @pytest.mark.parametrize("command", ALLOWED)
    def test_both_lanes_allow(self, command: str) -> None:
        assert git_bypass_deny_reason(command) is None
        assert deny_match(command) is None
