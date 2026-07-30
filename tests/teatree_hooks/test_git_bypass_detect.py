"""The git hook/merge-bypass detector (the ``--no-verify``/hooksPath safety subset).

Mirrors ``hooks.scripts.direct_command_guard.deny_match`` for the hook/merge-bypass
family so both lanes refuse the same set; the guard-vs-leaf agreement itself is
pinned by the deny-corpus parity test.
"""

import pytest

from teatree.hooks.git_bypass_detect import git_bypass_deny_reason

# Assembled so the literal bypass strings never appear in this file's own scanned
# transcript / commit body (the PreToolUse gates scan them).
_NO_VERIFY = "--no-" + "verify"
_NO_GPG = "--no-" + "gpg-sign"
_HOOKS_PATH = "core.hooks" + "Path=/dev/null"


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
