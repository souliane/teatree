"""The pure text predicates over a deferred question's own wording.

``is_tool_lack_selfreport`` is the leak defence: an agent stopping because its own
session was dispatched WITHOUT the shell / ``gh`` / checkout its work needs is
reporting a DISPATCH fault, not asking the owner to decide anything, so that reason is
recorded INTERNAL and never reaches the DM. ``question_fingerprint`` collapses
cosmetically-different clones of one question onto a single dedupe marker.
"""

import pytest

from teatree.core.modelkit.question_text import is_tool_lack_selfreport, question_fingerprint


class TestQuestionFingerprint:
    def test_whitespace_and_case_variants_share_a_fingerprint(self) -> None:
        assert question_fingerprint("I  lack   TOOLS ") == question_fingerprint("i lack tools")

    def test_different_questions_do_not_collide(self) -> None:
        assert question_fingerprint("ship it?") != question_fingerprint("revert it?")


class TestToolLackSelfReport:
    """An agent's own "I lack the tools to proceed" report is a dispatch fault (INTERNAL)."""

    @pytest.mark.parametrize(
        "text",
        [
            # The verbatim leak: a scanning-news park that reached the owner's DM.
            (
                "This session lacks any shell/write tool (no Bash, no Write/Edit, no gh) needed to run "
                "`manage.py shell -c record_candidate`, dedupe-check via `gh issue list`, or post the Slack DM."
            ),
            "I have no shell to run the management command.",
            "This agent runs shell-denied, so it cannot file the issue.",
            "This must be picked up by a session with the standard toolset.",
            "I need a session with the tools to complete this.",
            "Missing the gh CLI, so I can't open the PR.",
            # #201 (codex_reviewing) — leaked AFTER the first fix: the fault is
            # reported by its consequence, not the bare "no shell" token.
            (
                "This session was launched without Bash/Edit/Write/Agent tool access, so I cannot "
                "inspect the PR diff locally, make code changes, or run the required "
                "`t3 tool verify-gates` green-proof, nor post the codex adversarial review comment "
                "via `gh pr comment`. Need either the session relaunched with full tool access, or "
                "explicit guidance on how to proceed read-only."
            ),
            # #202 (reviewing) — leaked AFTER the first fix: the fault is reported as
            # its symptom (no shell, internal task-context tools returned nothing).
            (
                "Ticket 187's body/state and the full Slack thread weren't available in this phase "
                "(no shell, TaskGet/TaskList returned nothing for it), so I can't confirm what "
                "concrete action is being asked about beyond a generic acknowledgment."
            ),
            # Isolated signals each phrasing carries, asserted alone so the class is
            # caught even when only one signal is present.
            "I cannot inspect the PR diff locally.",
            "Need the session relaunched with full tool access.",
            "TaskGet/TaskList returned nothing for it.",
            "This phase has no accessible checkout of the repo.",
            "I cannot run the required verify-gates green-proof.",
        ],
    )
    def test_tool_lack_phrasings_are_classified(self, text: str) -> None:
        assert is_tool_lack_selfreport(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "Should I merge PR #7 or wait for the release branch?",
            "The design has two viable approaches — which do you prefer?",
            "I don't have enough context about the rollout plan to continue.",
            "Should I write the migration now or in a follow-up?",
            # Genuine review-phase decision — no tool-lack signal — must stay owner.
            "Two findings conflict; should I block the PR or file a follow-up ticket?",
            # Near-miss owner questions that brush the new signal words without being
            # a lack report: deciding to "check out" a branch, a "which tool" pick, a
            # "cannot decide" judgment gap.
            "Should I check out the release branch or stay on main?",
            "Which tool should I use to profile the endpoint?",
            "I cannot decide between the two migration strategies — which do you prefer?",
        ],
    )
    def test_genuine_owner_questions_are_not_classified(self, text: str) -> None:
        assert is_tool_lack_selfreport(text) is False

    def test_classification_ignores_case_and_whitespace(self) -> None:
        assert is_tool_lack_selfreport("  This  session  LACKS  any  SHELL  tool. ") is True
