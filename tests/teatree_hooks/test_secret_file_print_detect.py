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


class TestPerSegmentLexing:
    """Each statement is lexed independently — a redirect/verb on one segment must not mask another."""

    def test_redirect_on_an_unrelated_segment_does_not_suppress_the_leak(self) -> None:
        # The ``> /dev/null`` is on a DIFFERENT statement; the cat still prints.
        assert is_secret_print("cat ~/.ssh/id_rsa; echo ok > /dev/null") is True

    def test_print_verb_not_at_command_start_is_still_detected(self) -> None:
        # ``cat`` is the SECOND statement; the whole-command anchor missed it.
        assert is_secret_print("true; cat ~/.netrc") is True

    def test_secret_read_with_stderr_redirect_still_leaks(self) -> None:
        # ``2>`` redirects stderr, not stdout — the secret still hits stdout.
        assert is_secret_print("cat ~/.netrc 2> /tmp/err") is True

    def test_secret_read_redirected_to_a_file_is_captured(self) -> None:
        assert is_secret_print("cat ~/.ssh/id_rsa > /tmp/out") is False

    def test_downstream_consuming_sink_keeps_secret_off_stdout(self) -> None:
        assert is_secret_print("cat ~/.netrc | wc -l") is False

    def test_secret_read_inside_a_quoted_echo_arg_is_prose(self) -> None:
        assert is_secret_print('echo "reminder: cat ~/.netrc is forbidden"') is False
