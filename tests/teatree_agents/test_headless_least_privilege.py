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
