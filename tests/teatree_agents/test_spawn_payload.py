"""The pure ``execve`` payload arithmetic and the E2BIG naming (#4301)."""

from teatree.agents.spawn_payload import (
    MAX_ARG_STRLEN,
    SpawnPayload,
    arg_max_bytes,
    e2big_message,
    is_e2big,
    measure_spawn_payload,
    spawn_refusal_reason,
)


class TestArgMaxBytes:
    def test_reports_a_positive_limit(self) -> None:
        assert arg_max_bytes() >= MAX_ARG_STRLEN


class TestMeasureSpawnPayload:
    def test_charges_every_argument_and_env_entry(self) -> None:
        payload = measure_spawn_payload(["claude", "--model", "opus"], {"HOME": "/root"})
        # Each string costs its bytes plus a NUL and a pointer, argv and envp alike.
        assert payload.argv_bytes == len("claude--modelopus") + 3 * 9
        assert payload.env_bytes == len("HOME=/root") + 9
        assert payload.total_bytes == payload.argv_bytes + payload.env_bytes

    def test_reports_the_largest_single_argument(self) -> None:
        payload = measure_spawn_payload(["claude", "x" * 500], {})
        assert payload.largest_arg_bytes == 500

    def test_an_empty_spawn_is_measurable(self) -> None:
        payload = measure_spawn_payload([], {})
        assert payload.largest_arg_bytes == 0
        assert payload.total_bytes == 0

    def test_headroom_and_percentage_track_the_limit(self) -> None:
        payload = measure_spawn_payload(["claude"], {})
        assert payload.headroom_bytes == payload.total_limit_bytes - payload.total_bytes
        assert 0 <= payload.used_percent < 100


class TestSpawnRefusalReason:
    def test_a_fitting_payload_is_not_refused(self) -> None:
        assert spawn_refusal_reason(measure_spawn_payload(["claude"], {"A": "b"})) == ""

    def test_names_the_per_argument_limit_when_one_argument_is_oversized(self) -> None:
        payload = measure_spawn_payload(["claude", "x" * (MAX_ARG_STRLEN + 1)], {})
        reason = spawn_refusal_reason(payload)
        assert "single spawn argument" in reason
        assert str(MAX_ARG_STRLEN + 1) in reason
        assert "E2BIG" in reason

    def test_names_the_total_limit_when_the_whole_payload_is_oversized(self) -> None:
        # Each argument fits on its own; only the total breaches, which a
        # per-argument check would report as healthy.
        payload = SpawnPayload(
            argv_bytes=1_000_000,
            env_bytes=1_500_000,
            largest_arg_bytes=1024,
            total_limit_bytes=2_097_152,
        )
        reason = spawn_refusal_reason(payload)
        assert "argv+env limit" in reason
        assert "2500000" in reason


class TestIsE2big:
    def test_matches_the_sdk_wrapped_errno_text(self) -> None:
        assert is_e2big(
            "claude_agent_sdk._errors.CLIConnectionError: Failed to start Claude Code: "
            "[Errno 7] Argument list too long: '.../claude'"
        )

    def test_matches_the_bare_kernel_phrase(self) -> None:
        assert is_e2big("OSError: Argument list too long")

    def test_does_not_match_an_unrelated_failure(self) -> None:
        assert not is_e2big("CLIConnectionError: Failed to start Claude Code: [Errno 2] No such file")


class TestE2bigMessage:
    def test_names_the_cause_the_size_and_the_non_implication(self) -> None:
        payload = measure_spawn_payload(["claude", "x" * (MAX_ARG_STRLEN + 1)], {})
        message = e2big_message(payload)
        assert "could not be spawned" in message
        assert "E2BIG" in message
        assert str(MAX_ARG_STRLEN + 1) in message
        assert "nothing about the ticket's content is implicated" in message

    def test_reports_a_measurement_even_when_the_payload_looks_spawnable(self) -> None:
        # The kernel refused a payload our arithmetic says fits — say what was
        # measured rather than claim there is no problem.
        message = e2big_message(measure_spawn_payload(["claude"], {}))
        assert "which the kernel nonetheless refused" in message
        # Every E2BIG report names E2BIG — including the one our arithmetic did not predict.
        assert "E2BIG" in message


class TestGauge:
    def test_it_carries_both_limits(self) -> None:
        gauge = measure_spawn_payload(["claude"], {"A": "b"}).gauge()
        assert "largest argument" in gauge
        assert str(MAX_ARG_STRLEN) in gauge
