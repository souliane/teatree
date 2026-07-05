"""The secret-file-print detector leaf — mirrors the PreToolUse guard's matcher."""

import pytest

from teatree.hooks.secret_file_print_detect import is_secret_print, secret_print_deny_reason


class TestDenies:
    @pytest.mark.parametrize(
        "command",
        [
            "cat ~/.netrc",
            "cat ~/.ssh/id_rsa",
            "head -n1 /home/ci/.env",
            "tail secrets.env",
            "pass show ci/deploy-token",
            "echo glpat-abcdef123456",
            "printf ghp_deadbeefcafebabe0000",
            "echo 'gl" + "pat-fullyquotedtoken0'",  # a fully-quoted token still lands on stdout
        ],
    )
    def test_secret_prints_are_denied(self, command: str) -> None:
        assert is_secret_print(command) is True
        reason = secret_print_deny_reason(command)
        assert reason is not None
        assert "BLOCKED" in reason


class TestAllows:
    @pytest.mark.parametrize(
        "command",
        [
            "TOKEN=$(pass show ci/deploy-token)",  # captured into a variable
            "cat ~/.netrc > /tmp/out",  # redirected to a file
            "cat README.md",  # ordinary file
            "echo 'see ~/.netrc for the token path'",  # prose mentioning a path
            "pass show ci/token | gpg --encrypt",  # piped to a consuming sink
            'curl -H "Token: $TOKEN" https://api',  # value used via header, not printed
            "echo",  # bare echo, no argument
            "printf",  # bare printf, no argument
            "",
        ],
    )
    def test_captures_redirects_and_prose_pass(self, command: str) -> None:
        assert is_secret_print(command) is False
        assert secret_print_deny_reason(command) is None

    def test_pass_show_piped_to_re_emitter_is_still_a_print(self) -> None:
        # A pipe whose sink re-emits (cat/tee/…) still displays the secret.
        assert is_secret_print("pass show ci/token | cat") is True
