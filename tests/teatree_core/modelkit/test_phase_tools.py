from teatree.core.modelkit.phase_tools import ALL_TOOLS, disallowed_tools_for_phase, tools_for_phase


class TestToolsForPhase:
    def test_write_phase_gets_shell_and_write(self) -> None:
        coding = tools_for_phase("coding")
        assert {"shell", "write_file", "edit_file", "read_file"} <= coding

    def test_review_phase_is_read_only_no_write_no_shell(self) -> None:
        reviewing = tools_for_phase("reviewing")
        assert "read_file" in reviewing
        assert "write_file" not in reviewing
        assert "edit_file" not in reviewing
        assert "shell" not in reviewing

    def test_short_verb_spelling_resolves_same_as_gerund(self) -> None:
        assert tools_for_phase("review") == tools_for_phase("reviewing")
        assert tools_for_phase("code") == tools_for_phase("coding")

    def test_unknown_phase_falls_back_to_read_only_never_full(self) -> None:
        unknown = tools_for_phase("no-such-phase")
        assert "shell" not in unknown
        assert "write_file" not in unknown
        assert "read_file" in unknown

    def test_disallowed_is_the_exact_complement(self) -> None:
        for phase in ("coding", "reviewing", "planning", "shipping"):
            allowed = tools_for_phase(phase)
            disallowed = disallowed_tools_for_phase(phase)
            assert allowed | disallowed == ALL_TOOLS
            assert allowed & disallowed == frozenset()

    def test_review_phase_disallows_write_tools(self) -> None:
        assert {"write_file", "edit_file", "shell"} <= disallowed_tools_for_phase("reviewing")
