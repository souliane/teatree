"""agents.reader_profile (#116): the quarantined reader is tool-less and credential-less.

RED scenario 1 — the reader phase gets NO tools; Lane A denies every acting/exfil SDK
built-in. RED scenario 2 — the reader env carries no posting credential, and the
hermetic scrub removes secrets from ``os.environ`` for the spawn window and restores.
"""

import os

from teatree.agents.reader_profile import READER_PHASE, is_reader_phase, reader_child_env, reader_env_hermetic
from teatree.agents.sdk_tool_map import sdk_disallowed_tools_for_phase
from teatree.core.modelkit.phase_tools import ALL_TOOLS, disallowed_tools_for_phase, tools_for_phase


class TestReaderToolless:
    """RED scenario 1: the reader physically cannot act or exfiltrate."""

    def test_reader_phase_has_the_empty_toolset(self) -> None:
        assert tools_for_phase(READER_PHASE) == frozenset()

    def test_lane_a_denies_every_acting_and_exfil_sdk_tool(self) -> None:
        denied = set(sdk_disallowed_tools_for_phase(READER_PHASE))
        # every read / write / shell / fetch / spawn built-in is on the deny list
        assert {
            "Read",
            "Write",
            "Edit",
            "NotebookEdit",
            "Grep",
            "Glob",
            "Bash",
            "WebFetch",
            "WebSearch",
            "Agent",
            "Task",
        } <= denied

    def test_reader_disallows_the_full_capability_complement(self) -> None:
        # The empty allowance means the disallow set is the WHOLE capability universe.
        assert disallowed_tools_for_phase(READER_PHASE) == ALL_TOOLS

    def test_is_reader_phase_recognises_the_phase(self) -> None:
        assert is_reader_phase(READER_PHASE) is True
        assert is_reader_phase("coding") is False


class TestReaderChildEnvAllowlist:
    """RED scenario 2: the reader env drops every posting/forge/secret credential."""

    def test_posting_and_forge_credentials_do_not_survive(self) -> None:
        base = {
            "SLACK_BOT_TOKEN": "xoxb-secret",
            "GITHUB_TOKEN": "gh-secret",
            "GITLAB_TOKEN": "gl-secret",
            "T3_SECRET_RESOLVER": "resolver",
            "NOTION_API_KEY": "notion-secret",
            "SOME_OTHER_SECRET": "nope",
        }
        child = reader_child_env(base)
        assert child == {}

    def test_the_inference_credential_and_runtime_survive(self) -> None:
        base = {
            "ANTHROPIC_API_KEY": "sk-ant-inference",
            "PATH": "/usr/bin",
            "HOME": "/home/op",
            "SLACK_BOT_TOKEN": "xoxb-secret",
        }
        child = reader_child_env(base)
        assert child["ANTHROPIC_API_KEY"] == "sk-ant-inference"
        assert child["PATH"] == "/usr/bin"
        assert child["HOME"] == "/home/op"
        assert "SLACK_BOT_TOKEN" not in child

    def test_base_is_not_mutated(self) -> None:
        base = {"SLACK_BOT_TOKEN": "x", "PATH": "/usr/bin"}
        reader_child_env(base)
        assert base == {"SLACK_BOT_TOKEN": "x", "PATH": "/usr/bin"}


class TestReaderEnvHermetic:
    """RED scenario 2 (belt): the ``os.environ`` secret-strip for the spawn window."""

    def test_a_secret_is_absent_inside_and_restored_after(self) -> None:
        os.environ["SLACK_BOT_TOKEN"] = "xoxb-sentinel"
        try:
            with reader_env_hermetic():
                assert "SLACK_BOT_TOKEN" not in os.environ
            assert os.environ["SLACK_BOT_TOKEN"] == "xoxb-sentinel"
        finally:
            os.environ.pop("SLACK_BOT_TOKEN", None)

    def test_the_inference_credential_survives_inside_the_scrub(self) -> None:
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-inference"
        try:
            with reader_env_hermetic():
                assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-inference"
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)

    def test_a_non_secret_runtime_key_is_untouched(self) -> None:
        with reader_env_hermetic():
            assert "PATH" in os.environ
