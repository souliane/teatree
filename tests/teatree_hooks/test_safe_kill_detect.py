"""Tests for ``teatree.hooks.safe_kill_detect`` (#2225).

The PreToolUse gate denies a Bash command that signals a process by a raw,
guessed pid and routes the agent to the safe-kill CLI. These tests pin which
command shapes are flagged (raw ``kill <pid>`` at a command position) and which
are left alone (``kill -0`` probe, ``%job``/``$VAR``/``$(…)`` targets, ``pkill``
by name, a ``kill`` token in a comment/string/argument, non-kill commands).
"""

from teatree.hooks.safe_kill_detect import detect_raw_pid_kill


class TestRawPidKillFlagged:
    def test_plain_kill_pid(self) -> None:
        result = detect_raw_pid_kill("kill 4242")
        assert result.is_raw_pid_kill
        assert result.pid == 4242

    def test_kill_dash_nine_pid(self) -> None:
        result = detect_raw_pid_kill("kill -9 4242")
        assert result.is_raw_pid_kill
        assert result.pid == 4242

    def test_kill_sigkill_named_signal(self) -> None:
        result = detect_raw_pid_kill("kill -SIGKILL 4242")
        assert result.is_raw_pid_kill
        assert result.pid == 4242

    def test_kill_s_flag_signal(self) -> None:
        result = detect_raw_pid_kill("kill -s TERM 4242")
        assert result.is_raw_pid_kill
        assert result.pid == 4242

    def test_kill_process_group_negative_pid(self) -> None:
        result = detect_raw_pid_kill("kill -- -4242")
        assert result.is_raw_pid_kill
        assert result.pid == 4242

    def test_kill_after_separator_is_a_command_position(self) -> None:
        result = detect_raw_pid_kill("echo done && kill -9 4242")
        assert result.is_raw_pid_kill
        assert result.pid == 4242

    def test_signal_flag_is_not_parsed_as_the_pid(self) -> None:
        result = detect_raw_pid_kill("kill -9 5678")
        assert result.is_raw_pid_kill
        assert result.pid == 5678

    def test_message_routes_to_cli_and_sessions_path(self) -> None:
        result = detect_raw_pid_kill("kill -9 4242")
        assert "t3 teatree safe-kill" in result.message
        assert "~/.claude/sessions/*.json" in result.message
        assert "~/.claude/projects" not in result.message
        assert "looks idle" in result.message.lower() or "looked dead" in result.message.lower()


class TestNotFlagged:
    def test_kill_dash_zero_is_a_liveness_probe(self) -> None:
        result = detect_raw_pid_kill("kill -0 4242")
        assert not result.is_raw_pid_kill
        assert result.pid is None

    def test_kill_s_zero_is_a_liveness_probe(self) -> None:
        result = detect_raw_pid_kill("kill -s 0 4242")
        assert not result.is_raw_pid_kill

    def test_pkill_by_name_is_not_a_raw_pid_kill(self) -> None:
        result = detect_raw_pid_kill("pkill -9 chrome")
        assert not result.is_raw_pid_kill
        assert result.pid is None

    def test_killall_is_not_a_raw_pid_kill(self) -> None:
        result = detect_raw_pid_kill("killall node")
        assert not result.is_raw_pid_kill

    def test_kill_a_job_spec_is_not_a_raw_pid(self) -> None:
        result = detect_raw_pid_kill("kill %1")
        assert not result.is_raw_pid_kill

    def test_kill_a_variable_is_not_a_raw_pid(self) -> None:
        result = detect_raw_pid_kill("kill $PID")
        assert not result.is_raw_pid_kill

    def test_kill_command_substitution_target_is_not_a_raw_pid(self) -> None:
        result = detect_raw_pid_kill("kill -9 $(pgrep claude)")
        assert not result.is_raw_pid_kill

    def test_kill_in_a_comment_is_not_flagged(self) -> None:
        result = detect_raw_pid_kill("# kill 4242")
        assert not result.is_raw_pid_kill

    def test_kill_as_an_argument_is_not_flagged(self) -> None:
        result = detect_raw_pid_kill("grep kill 4242 file")
        assert not result.is_raw_pid_kill

    def test_kill_inside_a_string_is_not_flagged(self) -> None:
        result = detect_raw_pid_kill('echo "to kill: kill 1234"')
        assert not result.is_raw_pid_kill

    def test_kill_as_a_subcommand_word_is_not_flagged(self) -> None:
        result = detect_raw_pid_kill("git kill 5")
        assert not result.is_raw_pid_kill

    def test_non_kill_command(self) -> None:
        result = detect_raw_pid_kill("ps -axo pid,comm")
        assert not result.is_raw_pid_kill

    def test_empty_command(self) -> None:
        result = detect_raw_pid_kill("")
        assert not result.is_raw_pid_kill

    def test_kill_pid_zero_or_one_not_flagged(self) -> None:
        assert not detect_raw_pid_kill("kill 1").is_raw_pid_kill
        assert not detect_raw_pid_kill("kill 0").is_raw_pid_kill
