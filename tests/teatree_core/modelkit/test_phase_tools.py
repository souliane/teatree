from teatree.core.modelkit.phase_tools import ALL_TOOLS, disallowed_tools_for_phase, tools_for_phase


class TestToolsForPhase:
    def test_write_phase_gets_shell_and_write(self) -> None:
        coding = tools_for_phase("coding")
        assert {"shell", "write_file", "edit_file", "read_file"} <= coding

    def test_review_phase_has_shell_and_read_but_never_write(self) -> None:
        # F4: the reviewer skill requires the shell (cold-review checkout,
        # `t3 tool verify-gates`, `git log -S`, `t3 review post-comment`) to
        # produce a merge_safe/hold verdict — but never mutates source, so it
        # keeps the read-mostly-with-shell shape (no write/edit).
        reviewing = tools_for_phase("reviewing")
        assert {"read_file", "search_files", "shell"} <= reviewing
        assert "write_file" not in reviewing
        assert "edit_file" not in reviewing

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

    def test_review_phase_disallows_write_but_allows_shell(self) -> None:
        disallowed = disallowed_tools_for_phase("reviewing")
        assert {"write_file", "edit_file"} <= disallowed
        assert "shell" not in disallowed


class TestDispatchablePhaseTotality:
    """Every DIS-A/DIS-B dispatchable phase resolves an explicit, correct tool set."""

    def test_bughunt_executes_but_never_writes(self) -> None:
        bughunt = tools_for_phase("bughunt")
        assert {"shell", "dispatch_subtask", "read_file", "search_files"} <= bughunt
        assert "write_file" not in bughunt
        assert "edit_file" not in bughunt

    def test_debugging_gets_the_full_write_set(self) -> None:
        debugging = tools_for_phase("debugging")
        assert {"shell", "write_file", "edit_file", "read_file"} <= debugging

    def test_codex_review_has_shell_but_never_writes(self) -> None:
        # codex_reviewing runs the same shell-backed review flow as reviewing.
        tools = tools_for_phase("codex_reviewing")
        assert {"read_file", "shell"} <= tools
        assert "write_file" not in tools
        assert "edit_file" not in tools

    def test_adversarial_and_e2e_review_stay_read_mostly_no_shell(self) -> None:
        # The adversarial/e2e review phases are pure diff-read audits — no shell.
        for phase in ("codex_adversarial_reviewing", "e2e_reviewing"):
            tools = tools_for_phase(phase)
            assert "read_file" in tools, phase
            assert "write_file" not in tools, phase
            assert "shell" not in tools, phase
