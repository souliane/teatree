import pytest

from teatree.utils.run import PIPE, CommandFailedError, run_allowed_to_fail, run_checked, spawn


class TestRunChecked:
    def test_returns_completed_process_on_success(self) -> None:
        result = run_checked(["true"])
        assert result.returncode == 0

    def test_captures_stdout_as_text(self) -> None:
        result = run_checked(["sh", "-c", "printf hello"])
        assert result.stdout == "hello"
        assert result.stderr == ""

    def test_raises_command_failed_on_nonzero(self) -> None:
        with pytest.raises(CommandFailedError) as exc:
            run_checked(["false"])
        assert exc.value.returncode == 1
        assert exc.value.cmd == ["false"]

    def test_preserves_stderr_in_exception(self) -> None:
        with pytest.raises(CommandFailedError) as exc:
            run_checked(["sh", "-c", "echo boom >&2; exit 2"])
        assert "boom" in str(exc.value)
        assert exc.value.stderr.strip() == "boom"
        assert exc.value.returncode == 2

    def test_passes_env_and_cwd(self, tmp_path: object) -> None:
        result = run_checked(["sh", "-c", "echo $FOO"], env={"FOO": "bar"}, cwd=tmp_path)
        assert result.stdout.strip() == "bar"

    def test_feeds_stdin(self) -> None:
        result = run_checked(["cat"], stdin_text="hello\n")
        assert result.stdout == "hello\n"


class TestRunAllowedToFail:
    def test_returns_result_on_success(self) -> None:
        result = run_allowed_to_fail(["true"])
        assert result.returncode == 0

    def test_returns_result_when_code_expected(self) -> None:
        result = run_allowed_to_fail(["sh", "-c", "exit 2"], expected_codes=(0, 2))
        assert result.returncode == 2

    def test_raises_when_code_unexpected(self) -> None:
        with pytest.raises(CommandFailedError):
            run_allowed_to_fail(["sh", "-c", "exit 3"], expected_codes=(0, 1))

    def test_accepts_any_code_when_expected_is_none(self) -> None:
        result = run_allowed_to_fail(["sh", "-c", "exit 42"], expected_codes=None)
        assert result.returncode == 42


class TestCommandFailedError:
    def test_message_contains_command_and_stderr_tail(self) -> None:
        err = CommandFailedError(["docker", "rm", "-f", "nope"], 1, "", "Error: No such container: nope")
        msg = str(err)
        assert "docker rm -f nope" in msg
        assert "No such container: nope" in msg

    def test_message_when_streams_empty(self) -> None:
        err = CommandFailedError(["false"], 1, "", "")
        assert "command failed (rc=1)" in str(err)
        assert "false" in str(err)

    def test_redacts_authorization_header_value(self) -> None:
        err = CommandFailedError(
            ["gh", "api", "user", "--header", "Authorization: Bearer ghp_secrettoken"],
            1,
            "",
            "",
        )
        msg = str(err)
        assert "ghp_secrettoken" not in msg
        assert "Authorization: <redacted>" in msg

    def test_redacts_authorization_header_in_separate_arg(self) -> None:
        err = CommandFailedError(
            [
                "gh",
                "api",
                "user",
                "--header",
                "authorization: token mysupersecret",
            ],
            1,
            "",
            "",
        )
        msg = str(err)
        assert "mysupersecret" not in msg
        assert "<redacted>" in msg

    def test_redacts_token_query_parameters(self) -> None:
        err = CommandFailedError(
            ["curl", "https://example.com/api?token=verysecret&other=ok"],
            22,
            "",
            "",
        )
        msg = str(err)
        assert "verysecret" not in msg
        assert "token=<redacted>" in msg
        assert "other=ok" in msg

    def test_preserves_cmd_attribute_for_callers(self) -> None:
        original = ["gh", "api", "user", "--header", "Authorization: Bearer ghp_x"]
        err = CommandFailedError(original, 1, "", "")
        assert err.cmd == original


class TestSpawn:
    def test_returns_popen_that_can_be_awaited(self) -> None:
        proc = spawn(["sh", "-c", "exit 0"])
        proc.wait(timeout=5)
        assert proc.returncode == 0

    def test_capture_output_pipes_streams(self) -> None:
        proc = spawn(["sh", "-c", "echo hi"], stdout=PIPE, stderr=PIPE)
        stdout, _ = proc.communicate(timeout=5)
        assert stdout.strip() == "hi"
