"""Only a RULE memory can be recurred against, and only a HUMAN turn is a correction (#2663).

The accountant indexed every file under the memory dir as a rule and accepted any
user-shaped transcript line as a correction, so a multi-KB dispatch brief shared two
incidental tokens with a per-ticket state log and minted a "recurrence" against it —
an umbrella checkbox and a scheduled coding task for a memory that states no rule.
"""

from pathlib import Path

from django.test import SimpleTestCase

from teatree.loops.dream.compliance_attribution import _correction_lines, _memory_rules, is_rule_memory
from teatree.loops.dream.replay import ConsolidationExtract, WeightedSnippet

#: A per-ticket state log: `type: project`, no rule in it — the shape that minted the gap.
_PROJECT_LOG = (
    "---\n"
    "name: ticket-9001-fold-into-9002-pending-approval\n"
    'description: "Backlog sweep state: the fold proposal still awaits approval."\n'
    "metadata:\n"
    "  node_type: memory\n"
    "  type: project\n"
    "---\n"
    "The sweep proposes folding ticket 9001 into 9002; the approval is still pending.\n"
    "Do not re-propose it while the question is open. Re-check the worktree review state.\n"
)

_REFERENCE_NOTE = (
    "---\n"
    "name: writes-need-the-forge-token-exported\n"
    'description: "Where the forge token comes from."\n'
    "metadata:\n"
    "  node_type: memory\n"
    "  type: reference\n"
    "---\n"
    "Export the forge token from the secret store before any authenticated write.\n"
)

#: A genuine instruction: `type: feedback`, slug carries no legacy `feedback_` prefix.
_FEEDBACK_RULE = (
    "---\n"
    "name: no-unsolicited-progress-dms\n"
    'description: "Do not send routine progress DMs."\n'
    "metadata:\n"
    "  type: feedback\n"
    "---\n"
    "Never send a routine progress DM; only a blocker or a genuine ask reaches the owner.\n"
)

#: The frontmatter-less index files the memory glob also yields.
_INDEX_FILE = "# Auto Memory — Index\n\n- a-memory.md\n- another-memory.md\n"


def _memory(name: str, body: str) -> WeightedSnippet:
    return WeightedSnippet(path=Path(f"/memory/{name}"), kind="memory", weight=90, text=body)


def _turn(name: str, body: str, kind: str = "main") -> WeightedSnippet:
    return WeightedSnippet(path=Path(f"/sessions/{name}"), kind=kind, weight=100, text=body)


def _extract(*snippets: WeightedSnippet) -> ConsolidationExtract:
    return ConsolidationExtract(snippets=tuple(snippets))


class IsRuleMemoryTestCase(SimpleTestCase):
    """The rule universe is the memories that STATE a rule, not every file in the dir."""

    def test_project_state_log_is_not_a_rule(self) -> None:
        assert not is_rule_memory("ticket-9001-fold-into-9002-pending-approval", _PROJECT_LOG)

    def test_reference_note_is_not_a_rule(self) -> None:
        assert not is_rule_memory("writes-need-the-forge-token-exported", _REFERENCE_NOTE)

    def test_frontmatterless_index_file_is_not_a_rule(self) -> None:
        assert not is_rule_memory("MEMORY", _INDEX_FILE)
        assert not is_rule_memory("MEMORY_ARCHIVE", _INDEX_FILE)

    def test_typed_feedback_memory_is_a_rule(self) -> None:
        assert is_rule_memory("no-unsolicited-progress-dms", _FEEDBACK_RULE)

    def test_typed_user_memory_is_a_rule(self) -> None:
        body = "---\nname: owner-prefers-terse-output\nmetadata:\n  type: user\n---\nKeep replies terse.\n"
        assert is_rule_memory("owner-prefers-terse-output", body)

    def test_legacy_feedback_slug_without_frontmatter_is_still_a_rule(self) -> None:
        # The pre-frontmatter corpus carries the type in the slug; it must stay in the universe.
        assert is_rule_memory("feedback_askuserquestion_overuse", "name: feedback_askuserquestion_overuse\nRule.\n")

    def test_type_in_the_body_does_not_promote_a_project_log(self) -> None:
        body = (
            "---\nname: ticket-9001-notes\nmetadata:\n  type: project\n---\n"
            "The step failed with an unexpected type: feedback was requested by the reviewer.\n"
        )
        assert not is_rule_memory("ticket-9001-notes", body)

    def test_node_type_line_is_not_read_as_the_type(self) -> None:
        body = "---\nname: some-note\nmetadata:\n  node_type: memory\n---\nA note with no type.\n"
        assert not is_rule_memory("some-note", body)


class MemoryRuleUniverseTestCase(SimpleTestCase):
    """`_memory_rules` indexes only rule memories, so a non-rule can never be recurred against."""

    def test_only_rule_memories_are_indexed(self) -> None:
        extract = _extract(
            _memory("ticket-9001-fold-into-9002-pending-approval.md", _PROJECT_LOG),
            _memory("writes-need-the-forge-token-exported.md", _REFERENCE_NOTE),
            _memory("MEMORY.md", _INDEX_FILE),
            _memory("no-unsolicited-progress-dms.md", _FEEDBACK_RULE),
        )
        assert [rule.slug for rule in _memory_rules(extract)] == ["no-unsolicited-progress-dms"]


class CorrectionSourceTestCase(SimpleTestCase):
    """Only a human turn in a MAIN transcript is correction evidence."""

    def test_dispatch_brief_is_not_a_correction(self) -> None:
        brief = (
            '{"role": "user"} Work on ticket 9001. Issue: <issue-url> Current phase: coding '
            "Reason: CI is green at this head, so this is the only thing standing between the PR "
            "and merge. Do not cd out of the worktree; never force-push the branch.\n"
        )
        assert _correction_lines(_extract(_turn("s.jsonl", brief))) == []

    def test_subagent_brief_turn_is_not_a_correction(self) -> None:
        brief = '{"role": "user"} You are `cold-reviewer-r4`. Do not approve without a green run.\n'
        assert _correction_lines(_extract(_turn("a.jsonl", brief, kind="subagent"))) == []

    def test_task_output_turn_is_not_a_correction(self) -> None:
        brief = '{"role": "user"} Never skip the gate; stop if the run is red.\n'
        assert _correction_lines(_extract(_turn("t.output", brief, kind="task_output"))) == []

    def test_owner_slack_correction_is_kept(self) -> None:
        # The owner's DMs are a non-transcript member source; excluding by agent-kind
        # rather than allowlisting "main" is what keeps them reaching the detector.
        line = '{"role": "user"} you are spamming me again, stop the digest'
        assert _correction_lines(_extract(_turn("dm", line + "\n", kind="slack_dm"))) == [line]

    def test_genuine_human_correction_is_kept(self) -> None:
        line = '{"role": "user"} why are you spamming me on slack? stop it!!'
        assert _correction_lines(_extract(_turn("s.jsonl", line + "\n"))) == [line]
