import pytest

from teatree.core.modelkit.phase_tools import (
    ALL_TOOLS,
    VERDICT_REVIEW_PHASES,
    disallowed_tools_for_phase,
    tools_for_phase,
)


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

    @pytest.mark.parametrize("phase", sorted(VERDICT_REVIEW_PHASES))
    def test_every_verdict_review_phase_gets_the_shell_never_write(self, phase: str) -> None:
        # Each verdict-review phase's deliverable is a RECORDED verdict. The
        # `codex_*` variants have NO server-side envelope seam and the MCP post
        # path is GitLab-only, so on a GitHub PR the shell (`t3 teatree review
        # record` / `t3 teatree review post-comment`, bound to a `git rev-parse
        # HEAD` sha off a `git worktree add --detach` cold checkout) is their only
        # way to deliver — a shell-less codex member stalls and leaks an "I have no
        # Bash/git/gh" question to the owner. It still never mutates source.
        tools = tools_for_phase(phase)
        assert {"read_file", "search_files", "shell"} <= tools, phase
        assert "write_file" not in tools, phase
        assert "edit_file" not in tools, phase

    def test_verdict_review_phases_membership_is_exactly_the_four(self) -> None:
        # Pin the exact membership: the set drives the `dict.fromkeys` shell grant
        # in the table, so adding e.g. `scoping` here would silently hand it the
        # shell. This closes that gap — a membership change must update this test.
        assert (
            frozenset({"reviewing", "codex_reviewing", "codex_adversarial_reviewing", "e2e_reviewing"})
            == VERDICT_REVIEW_PHASES
        )

    def test_codex_review_variants_share_one_toolset(self) -> None:
        # Both variants come out of the SAME dispatch handler
        # (`loop.persistence._handle_codex_review`) for the same review work — the
        # `codex:adversarial-review` variant differs only in RUBRIC hardness, and
        # it is selected for the HIGHEST-stakes diffs (auth/, permissions/,
        # migrations/, secrets). Granting the harder review the weaker toolset is
        # the inversion this pins shut.
        assert tools_for_phase("codex_adversarial_reviewing") == tools_for_phase("codex_reviewing")

    def test_requesting_review_stays_read_only(self) -> None:
        # Anti-over-correction control: `requesting_review` posts no verdict, so it
        # is NOT in the verdict set and keeps the plain read-only grant.
        assert "shell" not in tools_for_phase("requesting_review")

    def test_architectural_review_gets_shell_but_never_writes(self) -> None:
        # The periodic ac-reviewing-codebase pass is a genuine review-WORK phase:
        # it walks the tree (Read/Grep), does git archaeology, and runs
        # `t3 tool verify-gates` — all of which need the shell. Without it the
        # dispatched review stalled and leaked an "I lack shell + no checkout"
        # question to the owner. Like the reviewer phases it never mutates source.
        tools = tools_for_phase("architectural_review")
        assert {"read_file", "search_files", "shell"} <= tools
        assert "write_file" not in tools
        assert "edit_file" not in tools
