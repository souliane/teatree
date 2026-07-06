"""Per-phase tool least-privilege at the headless harness (PR-11).

``_build_options`` injects the per-phase disallow list on the ``ClaudeAgentOptions``
it hands the SDK. A review-phase dispatch must be UNABLE to invoke git-write
tools — the cold-review lockout that keeps the transcript at its verdict, denied
at the harness. A write phase's options are byte-identical to before the lever.
"""

from django.test import TestCase

from teatree.agents._headless_options import _build_options, _disallowed_tools_for_phase
from teatree.core.models import Session, Task, Ticket


class TestDisallowedToolsForPhase(TestCase):
    def test_askuserquestion_always_denied(self) -> None:
        for phase in ("coding", "reviewing", "planning", "shipping"):
            assert "AskUserQuestion" in _disallowed_tools_for_phase(phase), phase

    def test_review_phase_denies_git_write_and_file_mutation(self) -> None:
        disallowed = set(_disallowed_tools_for_phase("reviewing"))
        # git-write runs through the shell -> Bash; file mutation via Write/Edit.
        assert {"Bash", "Write", "Edit"} <= disallowed

    def test_write_phase_denies_only_the_askuserquestion_floor(self) -> None:
        # A full-access phase adds nothing — byte-identical to before the lever.
        assert _disallowed_tools_for_phase("coding") == ["AskUserQuestion"]
        assert _disallowed_tools_for_phase("testing") == ["AskUserQuestion"]

    def test_sorted_and_deduplicated(self) -> None:
        result = _disallowed_tools_for_phase("reviewing")
        assert list(result) == sorted(result)
        assert len(result) == len(set(result))

    def test_reader_phase_denies_every_capability_and_the_extra_built_ins(self) -> None:
        # #116 (C1): the reader denies the full capability complement PLUS the built-ins
        # outside the capability vocabulary, so no tool of any kind remains.
        disallowed = set(_disallowed_tools_for_phase("directive_reading"))
        assert {"Read", "Bash", "WebFetch", "Agent", "Task", "Write", "Edit"} <= disallowed
        assert {"SlashCommand", "TodoWrite", "ExitPlanMode"} <= disallowed


class TestBuildOptionsHarnessPin(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()

    def _options_for(self, phase: str):
        session = Session.objects.create(ticket=self.ticket)
        task = Task.objects.create(ticket=self.ticket, session=session)
        return _build_options(task, "ctx", phase=phase, skills=[])

    def test_review_dispatch_cannot_invoke_git_write(self) -> None:
        options = self._options_for("reviewing")
        assert "Bash" in options.disallowed_tools
        assert "Write" in options.disallowed_tools
        assert "Edit" in options.disallowed_tools

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
        assert {"SlashCommand", "TodoWrite", "ExitPlanMode"} <= set(reader.disallowed_tools)

        coding = self._options_for("coding")
        assert coding.setting_sources is None
        assert coding.strict_mcp_config is False
