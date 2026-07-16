"""Per-phase tool least-privilege at the headless harness (PR-11).

``_build_options`` injects the per-phase disallow list on the ``ClaudeAgentOptions``
it hands the SDK. A review-phase dispatch must be UNABLE to invoke git-write
tools — the cold-review lockout that keeps the transcript at its verdict, denied
at the harness. A write phase's options are byte-identical to before the lever.
"""

from django.test import TestCase

from teatree.agents._headless_options import _build_options, _disallowed_tools_for_phase
from teatree.core.models import Session, Task, Ticket
from teatree.llm.builtin_tools import KNOWN_BUILTIN_TOOLS


class TestDisallowedToolsForPhase(TestCase):
    def test_askuserquestion_always_denied(self) -> None:
        for phase in ("coding", "reviewing", "planning", "shipping"):
            assert "AskUserQuestion" in _disallowed_tools_for_phase(phase), phase

    def test_review_phase_keeps_shell_but_denies_file_mutation(self) -> None:
        disallowed = set(_disallowed_tools_for_phase("reviewing"))
        # F4: the reviewer keeps the shell (Bash) to run the cold-review checkout,
        # verify-gates, and post the verdict; it never mutates source (Write/Edit).
        assert {"Write", "Edit"} <= disallowed
        assert "Bash" not in disallowed

    def test_write_phase_denies_only_the_askuserquestion_floor(self) -> None:
        # A full-access phase adds nothing — byte-identical to before the lever.
        assert _disallowed_tools_for_phase("coding") == ["AskUserQuestion"]
        assert _disallowed_tools_for_phase("testing") == ["AskUserQuestion"]

    def test_sorted_and_deduplicated(self) -> None:
        result = _disallowed_tools_for_phase("reviewing")
        assert list(result) == sorted(result)
        assert len(result) == len(set(result))

    def test_reader_phase_denies_the_exhaustive_known_builtin_registry(self) -> None:
        # #116 (C1): the reader denies EVERY known CLI built-in (the binary-validated
        # registry), so no built-in of any kind — including the external-effect
        # PushNotification/RemoteTrigger and tool-acquisition ToolSearch — remains
        # reachable. Anti-vacuous: dropping any built-in from the derivation → RED.
        disallowed = set(_disallowed_tools_for_phase("directive_reading"))
        assert set(KNOWN_BUILTIN_TOOLS) <= disallowed, set(KNOWN_BUILTIN_TOOLS) - disallowed
        assert {"PushNotification", "RemoteTrigger", "ToolSearch"} <= disallowed


class TestBuildOptionsHarnessPin(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()

    def _options_for(self, phase: str):
        session = Session.objects.create(ticket=self.ticket)
        task = Task.objects.create(ticket=self.ticket, session=session)
        return _build_options(task, "ctx", phase=phase, skills=[])

    def test_review_dispatch_keeps_shell_but_denies_file_mutation(self) -> None:
        options = self._options_for("reviewing")
        # The reviewer CAN shell out (checkout/verify-gates/post) but never writes.
        assert "Bash" not in options.disallowed_tools
        assert "Write" in options.disallowed_tools
        assert "Edit" in options.disallowed_tools

    def test_review_dispatch_reaches_the_teatree_mcp_review_tools(self) -> None:
        # Reviewing is a non-reader phase, so — unlike the #116 reader lockdown —
        # it loads the default settings sources (the project `.mcp.json` teatree MCP
        # server) with no strict-MCP restriction. The review MCP tools (github_pr_diff
        # / review_post_comment / task_complete) therefore reach the spawn, and none
        # are named in the built-in-only disallow list.
        options = self._options_for("reviewing")
        assert options.setting_sources is None
        assert options.strict_mcp_config is False
        for tool in (
            "mcp__teatree__github_pr_diff",
            "mcp__teatree__review_post_comment",
            "mcp__teatree__task_complete",
        ):
            assert tool not in options.disallowed_tools, tool

    def test_e2e_review_dispatch_cannot_invoke_git_write(self) -> None:
        assert "Bash" in self._options_for("e2e_reviewing").disallowed_tools

    def test_coding_dispatch_keeps_full_shell_access(self) -> None:
        options = self._options_for("coding")
        assert "Bash" not in options.disallowed_tools
        assert "Write" not in options.disallowed_tools
        assert options.disallowed_tools == ["AskUserQuestion"]

    def test_reader_dispatch_suppresses_all_tool_sources(self) -> None:
        # #116 (C1): an empty allowed_tools is a no-op in the SDK transport, so the reader
        # closes tool acquisition at the source — no settings, no MCP config — and denies
        # the extra built-ins. A coding dispatch is unaffected (loads settings as before).
        reader = self._options_for("directive_reading")
        assert reader.setting_sources == []
        assert reader.strict_mcp_config is True
        assert reader.mcp_servers == {}
        assert set(KNOWN_BUILTIN_TOOLS) <= set(reader.disallowed_tools)

        coding = self._options_for("coding")
        assert coding.setting_sources is None
        assert coding.strict_mcp_config is False

    def test_lifecycle_dispatch_wires_the_teatree_mcp_server(self) -> None:
        # #3242: plugin sub-agents ignore the mcpServers frontmatter, so the
        # headless lifecycle dispatch must inject the teatree local-stdio server
        # itself — otherwise coder/reviewer/shipper come up without mcp__teatree__*
        # and fall back to shelling out to the CLI for every structured read.
        for phase in ("coding", "reviewing", "testing", "shipping"):
            server = self._options_for(phase).mcp_servers.get("teatree")
            assert server is not None, phase
            assert server["command"] == "t3"
            assert list(server["args"]) == ["mcp", "serve"]

    def test_reader_dispatch_never_wires_the_teatree_mcp_server(self) -> None:
        # The quarantined reader stays hermetic: no MCP config of any origin,
        # including teatree's own server.
        assert self._options_for("directive_reading").mcp_servers == {}
