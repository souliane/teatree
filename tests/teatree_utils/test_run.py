import pytest

from teatree.utils.run import (
    PIPE,
    STREAMED_STDERR_RETAINED_LINES,
    CommandFailedError,
    run_allowed_to_fail,
    run_checked,
    run_streamed,
    spawn,
)


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


class TestRunStreamed:
    def test_returns_zero_on_success(self) -> None:
        assert run_streamed(["true"]) == 0

    def test_returns_code_when_check_disabled(self) -> None:
        assert run_streamed(["sh", "-c", "exit 3"], check=False) == 3

    def test_raises_with_returncode_on_nonzero(self) -> None:
        with pytest.raises(CommandFailedError) as exc:
            run_streamed(["false"])
        assert exc.value.returncode == 1
        assert exc.value.cmd == ["false"]

    def test_surfaces_subcommand_stderr_in_raised_error(self) -> None:
        # The failing subcommand's stderr must name itself in the error so the
        # next breakage is diagnosable, not a bare `command failed (rc=1)`.
        with pytest.raises(CommandFailedError) as exc:
            run_streamed(["sh", "-c", "echo 'unknown option --thread-ts' >&2; exit 1"])
        assert "unknown option --thread-ts" in exc.value.stderr
        assert "unknown option --thread-ts" in str(exc.value)

    def test_streams_stderr_to_parent_while_capturing(self, capfd: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(CommandFailedError):
            run_streamed(["sh", "-c", "echo live-stderr >&2; exit 1"])
        assert "live-stderr" in capfd.readouterr().err

    def test_captured_stderr_is_bounded(self, capfd: pytest.CaptureFixture[str]) -> None:
        # A long-lived check=False server (uvicorn/runserver) emits unbounded
        # stderr; the wrapper must retain only the last N lines, not every line.
        emitted = STREAMED_STDERR_RETAINED_LINES + 500
        script = f"for i in $(seq 1 {emitted}); do echo line-$i >&2; done; exit 1"
        with pytest.raises(CommandFailedError) as exc:
            run_streamed(["sh", "-c", script])
        retained = exc.value.stderr.splitlines()
        assert len(retained) == STREAMED_STDERR_RETAINED_LINES
        assert retained[-1] == f"line-{emitted}"
        assert retained[0] == f"line-{emitted - STREAMED_STDERR_RETAINED_LINES + 1}"
        assert capfd.readouterr().err.count("line-") == emitted


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

    def test_feeds_stdin(self) -> None:
        result = run_allowed_to_fail(["cat"], stdin_text="piped-in")
        assert result.stdout == "piped-in"

    def test_passes_env(self) -> None:
        result = run_allowed_to_fail(["sh", "-c", "echo $FOO"], env={"FOO": "bar"})
        assert result.stdout.strip() == "bar"


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
