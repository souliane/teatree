"""The coding directive carries the merge critic's checklist, built from the one source the contract uses."""

from django.test import TestCase

from teatree.agents.coding_prompt import _coding_phase_directive
from teatree.core.gates.merge_quality_gate import build_merge_quality_contract
from teatree.core.models import Ticket
from teatree.core.review.critic_rubric import (
    _MERGE_TRANSITION,
    EXHAUSTIVE_TEST_VALUE_RULE,
    llm_items,
    merge_item_questions,
)


class TestTheDirectiveCarriesTheMergeSelfCheck(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.directive = "\n".join(_coding_phase_directive())
        self.contract = build_merge_quality_contract(Ticket.objects.create(overlay="t3-teatree"), "a" * 40)

    def test_it_asks_every_merge_question(self) -> None:
        for item in llm_items(_MERGE_TRANSITION):
            assert f"{item.slug}: {item.adversarial_question}" in self.directive

    def test_it_states_the_exhaustive_citation_rule(self) -> None:
        assert "EVERY redundant or vacuous test in ONE `test_value` citation" in self.directive

    def test_the_questions_and_the_rule_are_the_ones_the_contract_renders(self) -> None:
        for shared in (merge_item_questions(), EXHAUSTIVE_TEST_VALUE_RULE):
            assert shared in self.directive
            assert shared in self.contract
