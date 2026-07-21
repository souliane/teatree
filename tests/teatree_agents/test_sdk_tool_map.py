"""Lane-A boundary map: teatree capability names -> claude_sdk tool names (PR-11).

The phase_tools SSOT names tools in teatree's provider-neutral vocabulary; Lane A
speaks the bundled ``claude`` CLI's names. These tests pin the review-phase
git-write denial, the write-phase no-op (byte-identical to before the lever), the
capability-coverage drift guard, and the parity guard that every mapped SDK name
is a real CLI built-in (a bogus deny-rule name is rejected by the CLI).
"""

import pytest

from teatree.agents.sdk_tool_map import CAPABILITY_TO_SDK_TOOLS, sdk_disallowed_tools_for_phase
from teatree.core.modelkit.phase_tools import ALL_TOOLS, VERDICT_REVIEW_PHASES
from teatree.eval.toolset import KNOWN_BUILTIN_TOOLS


class TestReviewPhaseKeepsShellDeniesWrite:
    def test_reviewing_disallows_write_edit_but_allows_shell(self) -> None:
        # F4: the reviewer needs the shell (Bash) for the cold-review checkout,
        # `t3 tool verify-gates`, and `t3 review post-comment`; it never mutates
        # source, so Write/Edit stay denied.
        disallowed = set(sdk_disallowed_tools_for_phase("reviewing"))
        assert {"Write", "Edit"} <= disallowed
        assert "Bash" not in disallowed

    def test_reviewing_allows_the_shell_family_and_denies_only_spawn(self) -> None:
        disallowed = set(sdk_disallowed_tools_for_phase("reviewing"))
        assert disallowed.isdisjoint({"Bash", "BashOutput", "KillBash", "KillShell"})
        # A cold reviewer never spawns further sub-agents.
        assert {"Agent", "Task"} <= disallowed

    def test_reviewing_keeps_read_tools(self) -> None:
        disallowed = set(sdk_disallowed_tools_for_phase("reviewing"))
        assert "Read" not in disallowed
        assert "Grep" not in disallowed
        assert "Glob" not in disallowed

    @pytest.mark.parametrize("phase", sorted(VERDICT_REVIEW_PHASES))
    def test_every_verdict_review_phase_keeps_bash_and_denies_write(self, phase: str) -> None:
        # The Lane-A translation of the SSOT: a phase that must RECORD a verdict
        # (`t3 review record` / `t3 review post-comment` off a cold checkout) is
        # dispatched with Bash reachable, and never with Write/Edit.
        disallowed = set(sdk_disallowed_tools_for_phase(phase))
        assert "Bash" not in disallowed, phase
        assert {"Write", "Edit"} <= disallowed, phase

    def test_requesting_review_denies_git_write(self) -> None:
        # Control: a non-verdict read-only phase still loses the whole shell family.
        assert "Bash" in sdk_disallowed_tools_for_phase("requesting_review")

    def test_short_verb_spelling_resolves_same(self) -> None:
        assert sdk_disallowed_tools_for_phase("review") == sdk_disallowed_tools_for_phase("reviewing")


class TestWritePhaseIsNoOp:
    def test_coding_disallows_nothing_extra(self) -> None:
        # A full-access phase's complement is empty -> no SDK names to deny, so the
        # dispatch options are byte-identical to before the least-privilege lever.
        assert sdk_disallowed_tools_for_phase("coding") == ()

    def test_testing_and_e2e_disallow_nothing_extra(self) -> None:
        assert sdk_disallowed_tools_for_phase("testing") == ()
        assert sdk_disallowed_tools_for_phase("e2e") == ()


class TestDeterministicOutput:
    def test_sorted_and_deduplicated(self) -> None:
        result = sdk_disallowed_tools_for_phase("reviewing")
        assert list(result) == sorted(result)
        assert len(result) == len(set(result))


class TestMapIntegrity:
    def test_every_capability_is_mapped(self) -> None:
        # Drift guard: a capability added to ALL_TOOLS without an SDK mapping would
        # silently drop from the disallow list.
        assert set(CAPABILITY_TO_SDK_TOOLS) == set(ALL_TOOLS)

    def test_every_mapped_name_is_a_known_cli_builtin(self) -> None:
        # A deny rule naming a tool the CLI does not register is rejected — pin
        # every mapped name against the validated built-in registry.
        mapped = {name for names in CAPABILITY_TO_SDK_TOOLS.values() for name in names}
        assert mapped <= set(KNOWN_BUILTIN_TOOLS), mapped - set(KNOWN_BUILTIN_TOOLS)

    def test_teatree_native_capabilities_map_to_no_sdk_tool(self) -> None:
        assert CAPABILITY_TO_SDK_TOOLS["recall_memory"] == frozenset()
        assert CAPABILITY_TO_SDK_TOOLS["record_attempt"] == frozenset()
