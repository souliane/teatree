"""Tests for ``teatree.hooks.safe_kill_detect`` (#2225).

The PreToolUse gate denies a Bash command that signals a process by a raw,
guessed pid and routes the agent to the safe-kill helper. These tests pin which
command shapes are flagged (raw ``kill <pid>``) and which are left alone
(``%job`` targets, ``pkill`` by name, non-kill commands).
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

    def test_message_routes_to_helper_and_session_id(self) -> None:
        result = detect_raw_pid_kill("kill -9 4242")
        assert "safe_kill" in result.message
        assert "session id" in result.message.lower()
        assert "looks idle" in result.message.lower() or "looked dead" in result.message.lower()


class TestNotFlagged:
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

    def test_non_kill_command(self) -> None:
        result = detect_raw_pid_kill("ps -axo pid,comm")
        assert not result.is_raw_pid_kill

    def test_empty_command(self) -> None:
        result = detect_raw_pid_kill("")
        assert not result.is_raw_pid_kill

    def test_kill_pid_zero_or_one_not_flagged(self) -> None:
        assert not detect_raw_pid_kill("kill 1").is_raw_pid_kill
        assert not detect_raw_pid_kill("kill 0").is_raw_pid_kill
