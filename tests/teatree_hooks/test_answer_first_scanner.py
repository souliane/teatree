"""Tests for the answer-first detector — a user's question is answered, not actioned.

The counterpart the structured-question gate never had. That gate fires when the
AGENT poses a question without the structured tool; this one fires on the inverse
— the USER asks and the agent replies with a delegation report instead of an
answer. The recorded failure: asked "are you going to merge #4001 or not??" the
agent dispatched a lane and reported the dispatch, so the owner had to ask again.

Anti-vacuity is the point of the split classes below: the block class carries the
recorded shape, and the pass class carries the over-block controls (an answered
question, a dispatch with no question, a question with no delegation, an honest
"I don't know yet"). A detector that fires on those would be worse than none.
"""

from teatree.hooks.answer_first_scanner import find_unanswered_question

# The recorded instance: an interrogative treated as an instruction.
_QUESTION = "are you going to merge 4001 or not??"
_DISPATCH_ONLY = "Dispatched a lane to merge #4001.\nIt is queued behind the current tick and will pick it up next.\n"


class TestFiresOnActionInsteadOfAnswer:
    def test_polar_question_answered_with_a_dispatch_report_fires(self) -> None:
        verdict = find_unanswered_question(_QUESTION, _DISPATCH_ONLY)

        assert verdict is not None
        assert "or not" in verdict.question.lower()
        assert "dispatch" in verdict.action.lower()

    def test_why_question_answered_with_a_dispatch_report_fires(self) -> None:
        user = "what were the problems with 4001? why wasn't it mergeable as is?"

        verdict = find_unanswered_question(user, "Handed it off to a reviewer sub-agent to investigate.")

        assert verdict is not None

    def test_explanation_demand_without_question_mark_fires(self) -> None:
        user = "tell me why it was not mergeable, please"

        verdict = find_unanswered_question(user, "Spawned an agent to look into it.")

        assert verdict is not None


class TestDoesNotFireOnLegitimateTurns:
    def test_direct_answer_alongside_the_dispatch_passes(self) -> None:
        agent = "Yes — merging it now. Dispatched a lane to run the merge command.\n"

        assert find_unanswered_question(_QUESTION, agent) is None

    def test_causal_answer_passes(self) -> None:
        user = "why wasn't it mergeable as is?"
        agent = (
            "It was not mergeable because ticket.py grew to 523 LOC over the 500 cap, "
            "and the module-health ratchet only lets an over-cap file shrink.\n"
            "Dispatched a lane to split it.\n"
        )

        assert find_unanswered_question(user, agent) is None

    def test_honest_unknown_passes(self) -> None:
        agent = "I do not know yet — I have not read the logs. Dispatched a lane to fetch them.\n"

        assert find_unanswered_question(_QUESTION, agent) is None

    def test_dispatch_with_no_user_question_passes(self) -> None:
        assert find_unanswered_question("merge 4001 please", _DISPATCH_ONLY) is None

    def test_question_answered_without_any_delegation_passes(self) -> None:
        assert find_unanswered_question(_QUESTION, "Running the merge myself right now.\n") is None

    def test_rhetorical_question_mark_without_a_cue_passes(self) -> None:
        assert find_unanswered_question("ship it (4001?)", _DISPATCH_ONLY) is None

    def test_question_inside_fenced_code_passes(self) -> None:
        user = "add this pattern\n```\nrg 'are you going to .*\\?'\n```\n"

        assert find_unanswered_question(user, _DISPATCH_ONLY) is None

    def test_empty_input_passes(self) -> None:
        assert find_unanswered_question("", _DISPATCH_ONLY) is None
        assert find_unanswered_question(_QUESTION, "") is None
