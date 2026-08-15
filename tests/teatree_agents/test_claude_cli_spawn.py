"""The ``claude`` CLI spawn: the system prompt travels by file, never by argv (#4301).

The regression these pin: a system-prompt append past the kernel's per-argument cap made
``execve`` refuse the child with ``[Errno 7]``, killing the phase before any work started.
Every "does it spawn" assertion below runs a REAL ``execve`` with the rendered argv and
environment — the only oracle that cannot be satisfied by an argv the kernel would reject
— and each is paired with the pre-fix inline shape as its control.
"""

import subprocess
import sys
from pathlib import Path

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk.types import SystemPromptPreset

from teatree.agents.claude_cli_spawn import (
    APPEND_PROMPT_FILE_FLAG,
    assert_spawnable,
    child_env,
    option_argument_strings,
    preflight_payload,
    prepared_spawn,
    spawn_error,
)
from teatree.agents.spawn_payload import MAX_ARG_STRLEN, AgentSpawnError, measure_spawn_payload

#: Past the 32-page per-argument cap, so the pre-fix inline shape is refused by execve.
_OVERSIZED_APPEND = "# Loaded skills\n\n" + "x" * (MAX_ARG_STRLEN + 50_000)
#: A harmless stand-in for the CLI binary, so the exec proves the PAYLOAD is acceptable.
_HARMLESS_BINARY = "/bin/true"

_linux_only = pytest.mark.skipif(sys.platform != "linux", reason="MAX_ARG_STRLEN is a Linux per-argument cap")


def _rendered_argv(options: ClaudeAgentOptions) -> list[str]:
    """The argv the REAL SDK transport would spawn — the only truthful source.

    Production measures a floor from its own options instead of reaching into this
    private builder; the tests use the real thing, because "does the kernel accept it"
    is a question only the actual command can answer.
    """
    from claude_agent_sdk._internal.transport.subprocess_cli import (  # noqa: PLC0415 — deferred: the SDK internal stays out of module import
        SubprocessCLITransport,
    )

    transport = SubprocessCLITransport(prompt="", options=options)
    transport._cli_path = "/usr/local/bin/claude"
    return list(transport._build_command())


def _options(append: str) -> ClaudeAgentOptions:
    """The shape ``_build_options`` produces: the claude_code preset plus an append."""
    return ClaudeAgentOptions(
        system_prompt=SystemPromptPreset(
            type="preset",
            preset="claude_code",
            append=append,
            exclude_dynamic_sections=True,
        ),
        disallowed_tools=["AskUserQuestion", "SendMessage"],
    )


def _exec_payload(argv: list[str]) -> subprocess.CompletedProcess[bytes]:
    """Hand the kernel the real argv (binary swapped for a harmless one) plus the real env."""
    return subprocess.run([_HARMLESS_BINARY, *argv[1:]], env=dict(child_env(_options(""))), check=False)


class TestPreparedSpawnMovesThePromptOffArgv:
    def test_the_append_is_written_to_a_file_the_flag_points_at(self) -> None:
        with prepared_spawn(_options("skill body")) as spawn_options:
            path = Path(spawn_options.extra_args[APPEND_PROMPT_FILE_FLAG] or "")
            assert path.read_text(encoding="utf-8") == "skill body"

    def test_the_claude_code_preset_survives_the_move(self) -> None:
        # Dropping the preset would silently replace Claude Code's own system prompt on
        # every headless run — the reason --system-prompt-file is NOT the flag used.
        with prepared_spawn(_options("skill body")) as spawn_options:
            argv = _rendered_argv(spawn_options)
            assert "--system-prompt" not in argv
            assert "--system-prompt-file" not in argv
            assert f"--{APPEND_PROMPT_FILE_FLAG}" in argv
            assert spawn_options.system_prompt == {
                "type": "preset",
                "preset": "claude_code",
                "exclude_dynamic_sections": True,
            }

    def test_the_prompt_body_never_appears_as_an_argument(self) -> None:
        with prepared_spawn(_options(_OVERSIZED_APPEND)) as spawn_options:
            argv = _rendered_argv(spawn_options)
            assert all(_OVERSIZED_APPEND not in arg for arg in argv)

    def test_the_prompt_file_dies_with_the_spawn(self) -> None:
        with prepared_spawn(_options("skill body")) as spawn_options:
            path = Path(spawn_options.extra_args[APPEND_PROMPT_FILE_FLAG] or "")
        assert not path.exists()

    def test_options_without_a_preset_append_are_yielded_unchanged(self) -> None:
        options = ClaudeAgentOptions(system_prompt="a plain string prompt")
        with prepared_spawn(options) as spawn_options:
            assert spawn_options is options

    def test_a_preset_with_no_append_is_yielded_unchanged(self) -> None:
        options = ClaudeAgentOptions(system_prompt=SystemPromptPreset(type="preset", preset="claude_code"))
        with prepared_spawn(options) as spawn_options:
            assert spawn_options is options


class TestAnOversizedPromptRemainsSpawnable:
    @_linux_only
    def test_control_the_pre_fix_inline_shape_is_refused_by_execve(self) -> None:
        # The defect itself: without this RED the assertions below would pass against
        # any argv, including one the kernel rejects.
        argv = _rendered_argv(_options(_OVERSIZED_APPEND))
        assert any(len(arg.encode()) > MAX_ARG_STRLEN for arg in argv)
        with pytest.raises(OSError, match="Argument list too long"):
            _exec_payload(argv)

    @_linux_only
    def test_the_prepared_shape_is_accepted_by_execve(self) -> None:
        with prepared_spawn(_options(_OVERSIZED_APPEND)) as spawn_options:
            argv = _rendered_argv(spawn_options)
            assert _exec_payload(argv).returncode == 0

    def test_no_rendered_argument_exceeds_the_per_argument_limit(self) -> None:
        with prepared_spawn(_options(_OVERSIZED_APPEND)) as spawn_options:
            argv = _rendered_argv(spawn_options)
            assert argv
            assert max(len(arg.encode()) for arg in argv) <= MAX_ARG_STRLEN


class TestTheBundledCliAcceptsTheFlag:
    """The offload is only a fix if the pinned SDK's bundled CLI knows the flag.

    A CLI that rejected it would turn every dispatch into an unknown-option death, so
    this goes red the moment an SDK bump drops the flag rather than at the next spawn.
    """

    def test_append_system_prompt_file_is_a_known_option(self) -> None:
        from claude_agent_sdk._internal.transport.subprocess_cli import (  # noqa: PLC0415 — deferred: the SDK internal stays out of module import
            SubprocessCLITransport,
        )

        cli = SubprocessCLITransport(prompt="", options=ClaudeAgentOptions())._find_cli()
        result = subprocess.run(
            [cli, f"--{APPEND_PROMPT_FILE_FLAG}", "/nonexistent-t3-probe.md", "-p", "hi"],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        output = f"{result.stdout}{result.stderr}".lower()
        assert "unknown option" not in output
        assert "not found" in output


class TestOptionArgumentStrings:
    """The production measurement is a floor over teatree's OWN argument strings."""

    def test_it_covers_every_string_that_grows_with_configuration(self) -> None:
        options = ClaudeAgentOptions(
            system_prompt="a prompt",
            disallowed_tools=["AskUserQuestion"],
            add_dirs=["/work/tree"],
            cwd="/work/tree",
            model="opus",
            extra_args={"some-flag": "some-value"},
        )
        strings = option_argument_strings(options)
        assert "a prompt" in strings
        assert "AskUserQuestion" in strings
        assert "/work/tree" in strings
        assert "opus" in strings
        assert "some-flag=some-value" in strings

    def test_it_never_over_reports_by_inventing_empty_arguments(self) -> None:
        assert all(strings for strings in option_argument_strings(ClaudeAgentOptions()))

    def test_the_offloaded_prompt_leaves_only_its_path_in_the_measurement(self) -> None:
        # The point of the fix, seen from the measurement side: the body is gone.
        with prepared_spawn(_options(_OVERSIZED_APPEND)) as spawn_options:
            assert max(len(s.encode()) for s in option_argument_strings(spawn_options)) <= MAX_ARG_STRLEN


class TestAssertSpawnable:
    def test_an_ordinary_spawn_returns_its_measurement(self) -> None:
        with prepared_spawn(_options("body")) as spawn_options:
            assert assert_spawnable(spawn_options).total_bytes > 0

    def test_a_measured_oversize_payload_is_refused_by_name(self) -> None:
        with pytest.raises(AgentSpawnError, match="E2BIG"):
            assert_spawnable(_options(_OVERSIZED_APPEND), {})

    def test_an_oversized_environment_is_refused_too(self) -> None:
        # envp shares the budget; a payload can breach with no oversized argument at all.
        with pytest.raises(AgentSpawnError, match="argv\\+env limit"):
            assert_spawnable(_options("body"), {"BIG": "x" * 10_000_000})


class TestSpawnError:
    def test_names_e2big_through_the_sdk_wrapper_chain(self) -> None:
        payload = measure_spawn_payload(["claude", "x" * (MAX_ARG_STRLEN + 1)], {})
        wrapper = RuntimeError("Failed to start Claude Code")
        wrapper.__cause__ = OSError(7, "Argument list too long", "/path/to/claude")

        named = spawn_error(wrapper, payload)

        assert named is not None
        assert "could not be spawned" in str(named)

    def test_returns_none_for_any_other_startup_failure(self) -> None:
        payload = measure_spawn_payload(["claude"], {})
        assert spawn_error(RuntimeError("Failed to start Claude Code: [Errno 2] No such file"), payload) is None

    def test_reports_the_measured_size_rather_than_an_errno(self) -> None:
        payload = preflight_payload(_options("body"))
        named = spawn_error(OSError(7, "Argument list too long"), payload)
        assert named is not None
        assert str(payload.total_bytes) in str(named)
