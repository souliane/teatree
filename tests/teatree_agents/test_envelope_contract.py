"""The headless brief teaches the result-envelope contract on EVERY phase (#3660).

A model that never saw teatree's prompts does not infer the envelope from
surrounding prose — on the metered router lane every headless task ran real
inference and then failed ``no_result_envelope``. These assert the brief states
the contract itself: required keys, allowed values, and a literal example whose
shape actually satisfies the phase evidence gate.
"""

import json

from django.test import SimpleTestCase, TestCase

from teatree.agents.envelope_contract import CONTRACT_HEADING, allowed_keys, envelope_contract_lines, envelope_example
from teatree.agents.prompt import build_system_context
from teatree.agents.result_schema import RESULT_JSON_SCHEMA, check_evidence, required_evidence_for_phase
from teatree.core.models import Session, Task, Ticket

_WORK_PHASE = "coding"
_VERIFICATION_PHASE = "reviewing"


def _contract_example(text: str) -> dict[str, object]:
    """The JSON example embedded in the contract block of *text*."""
    block = text.split(CONTRACT_HEADING, 1)[1]
    start = block.index("{")
    decoded, _ = json.JSONDecoder().raw_decode(block, start)
    assert isinstance(decoded, dict)
    return decoded


class TestEnvelopeContractText(SimpleTestCase):
    def test_allowed_keys_come_from_the_schema(self) -> None:
        properties = RESULT_JSON_SCHEMA["properties"]
        assert isinstance(properties, dict)
        assert set(allowed_keys()) == set(properties)

    def test_every_phase_states_the_contract_and_names_its_evidence_key(self) -> None:
        for phase in ("coding", "reviewing", "testing", "planning", "shipping", "answering", "scanning_news"):
            text = "\n".join(envelope_contract_lines(phase))
            assert CONTRACT_HEADING in text, phase
            assert "no_result_envelope" in text, phase
            for field in required_evidence_for_phase(phase):
                assert f"`{field}`" in text, (phase, field)

    def test_example_satisfies_the_phase_evidence_gate(self) -> None:
        # A brief whose example would itself be refused teaches the wrong shape.
        for phase in ("coding", "reviewing", "testing", "planning", "shipping", "answering", "scanning_news"):
            assert check_evidence(envelope_example(phase), phase) == "", phase

    def test_example_uses_only_schema_declared_keys(self) -> None:
        for phase in ("coding", "reviewing", "shipping"):
            assert set(envelope_example(phase)) <= set(allowed_keys()), phase


class TestSystemContextCarriesTheContract(TestCase):
    def _context(self, phase: str) -> str:
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/3660")
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session, phase=phase)
        return build_system_context(task, skills=[])

    def test_work_phase_brief_teaches_keys_and_example(self) -> None:
        context = self._context(_WORK_PHASE)
        assert CONTRACT_HEADING in context
        assert "files_modified" in context
        assert check_evidence(_contract_example(context), _WORK_PHASE) == ""

    def test_verification_phase_brief_teaches_keys_and_example(self) -> None:
        context = self._context(_VERIFICATION_PHASE)
        assert CONTRACT_HEADING in context
        assert "review_verdict" in context
        assert check_evidence(_contract_example(context), _VERIFICATION_PHASE) == ""

    def test_phase_without_required_evidence_still_teaches_the_contract(self) -> None:
        context = self._context("scoping")
        assert CONTRACT_HEADING in context
        assert check_evidence(_contract_example(context), "scoping") == ""
