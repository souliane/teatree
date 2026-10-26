import re
from unittest.mock import patch

from django.test import TestCase

from teatree.core.models import ConfigSetting, Rubric, Session, Ticket
from teatree.core.models.rubric import FAILING_RUN_CRITERIA, PHASE_CRITERIA


def _seeded_texts(ticket: Ticket, phase: str) -> list[str]:
    Session.objects.create(ticket=ticket).visit_phase(phase)
    rubric = Rubric.objects.active_for_ticket(ticket)
    return [] if rubric is None else [criterion.text for criterion in rubric.criteria]


class TestTheWithholdingFollowsTheCriterionText(TestCase):
    def test_a_failing_run_text_re_keyed_under_another_phase_is_withheld_when_switched_off(self) -> None:
        ConfigSetting.objects.set_value("forced_repro_gate_enabled", value=False)
        with patch.dict(PHASE_CRITERIA, {"rekeyed": PHASE_CRITERIA["fix"]}):
            assert _seeded_texts(Ticket.objects.create(overlay="t3-teatree"), "rekeyed") == []

    def test_a_failing_run_text_re_keyed_under_another_phase_is_seeded_when_switched_on(self) -> None:
        with patch.dict(PHASE_CRITERIA, {"rekeyed": PHASE_CRITERIA["fix"]}):
            assert _seeded_texts(Ticket.objects.create(overlay="t3-teatree"), "rekeyed") == [PHASE_CRITERIA["fix"]]

    def test_a_text_that_demands_no_failing_run_is_seeded_under_any_key_when_switched_off(self) -> None:
        ConfigSetting.objects.set_value("forced_repro_gate_enabled", value=False)
        with patch.dict(PHASE_CRITERIA, {"rekeyed": PHASE_CRITERIA["design"]}):
            assert _seeded_texts(Ticket.objects.create(overlay="t3-teatree"), "rekeyed") == [PHASE_CRITERIA["design"]]

    def test_every_phase_text_that_mentions_a_failing_run_is_withheld_by_the_switch(self) -> None:
        mentions_a_failing_run = {text for text in PHASE_CRITERIA.values() if re.search(r"\bfail", text)}
        assert mentions_a_failing_run == FAILING_RUN_CRITERIA
