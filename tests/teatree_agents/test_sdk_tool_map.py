"""Lane-A boundary map: teatree capability names -> claude_sdk tool names (PR-11).

The phase_tools SSOT names tools in teatree's provider-neutral vocabulary; Lane A
speaks the bundled ``claude`` CLI's names. These tests pin the review-phase
git-write denial, the write-phase no-op (byte-identical to before the lever), the
capability-coverage drift guard, and the parity guard that every mapped SDK name
is a real CLI built-in (a bogus deny-rule name is rejected by the CLI).
"""

from teatree.agents.sdk_tool_map import CAPABILITY_TO_SDK_TOOLS, sdk_disallowed_tools_for_phase
from teatree.core.modelkit.phase_tools import ALL_TOOLS
from teatree.eval.toolset import KNOWN_BUILTIN_TOOLS


class TestReviewPhaseDeniesGitWrite:
    def test_reviewing_disallows_bash_write_edit(self) -> None:
        # git-write runs through the shell -> Bash; file mutation via Write/Edit.
        disallowed = set(sdk_disallowed_tools_for_phase("reviewing"))
        assert {"Bash", "Write", "Edit"} <= disallowed

    def test_reviewing_disallows_shell_family_and_spawn(self) -> None:
        disallowed = set(sdk_disallowed_tools_for_phase("reviewing"))
        assert {"Bash", "BashOutput", "KillBash", "KillShell"} <= disallowed
        assert {"Agent", "Task"} <= disallowed

    def test_reviewing_keeps_read_tools(self) -> None:
        disallowed = set(sdk_disallowed_tools_for_phase("reviewing"))
        assert "Read" not in disallowed
        assert "Grep" not in disallowed
        assert "Glob" not in disallowed

    def test_e2e_reviewing_and_requesting_review_deny_git_write(self) -> None:
        for phase in ("e2e_reviewing", "requesting_review"):
            assert "Bash" in sdk_disallowed_tools_for_phase(phase), phase

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
