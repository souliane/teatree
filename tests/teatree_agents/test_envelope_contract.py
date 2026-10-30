"""The headless brief teaches the result-envelope contract on EVERY phase (#3660).

A model that never saw teatree's prompts does not infer the envelope from
surrounding prose — on the metered router lane every headless task ran real
inference and then failed ``no_result_envelope``. These assert the brief states
the contract itself: required keys, allowed values, and a literal example whose
shape actually satisfies the phase evidence gate.
"""

import json
import re
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from teatree.agents.envelope_contract import (
    CONTRACT_HEADING,
    allowed_keys,
    envelope_contract_lines,
    envelope_example,
    final_output_reminder_line,
)
from teatree.agents.prompt import build_system_context
from teatree.agents.result_schema import RESULT_JSON_SCHEMA, check_evidence, required_evidence_for_phase
from teatree.core.modelkit import question_card
from teatree.core.modelkit.owner_decision import OWNER_RAISABLE_DECISIONS
from teatree.core.modelkit.review_contract import VERDICT_CHECKS_RULE
from teatree.core.models import Session, Task, Ticket

_WORK_PHASE = "coding"
_VERIFICATION_PHASE = "reviewing"


def _contract_example(text: str) -> dict[str, object]:
    """The JSON example embedded in the contract block of *text*."""
    marker = "Minimal valid envelope for this phase"
    block = text.split(CONTRACT_HEADING, 1)[1].split(marker, 1)[1]
    start = block.index("{")
    decoded, _ = json.JSONDecoder().raw_decode(block, start)
    assert isinstance(decoded, dict)
    return decoded


#: What the brief states when each question_card limit is patched to 7.
_SENTINEL_FRAGMENTS = {
    "MAX_CHECKED_FACTS": "1-7 facts",
    "MAX_QUESTION_CHARS": "question max 7 chars",
    "MAX_SENTENCE_CHARS": "max 7 chars, one line",
    "MAX_WORDS": "max 7 words total",
    "MAX_LABEL_CHARS": "labels unique/nonblank, max 7 chars",
    "MAX_CONSEQUENCE_CHARS": "consequences max 7 chars",
    "MIN_OPTIONS": "0 or 7-4",
    "MAX_OPTIONS": "0 or 2-7",
}


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

    def test_every_phase_names_the_raisable_owner_decision_kinds(self) -> None:
        text = "\n".join(envelope_contract_lines(_WORK_PHASE))
        assert "`user_input_kind`" in text
        for decision in OWNER_RAISABLE_DECISIONS:
            assert decision.value in text

    def test_an_owner_stop_needs_the_checked_facts_and_everything_else_is_decided(self) -> None:
        text = "\n".join(envelope_contract_lines(_WORK_PHASE))
        assert "`user_input_checked`" in text
        assert "access or permission grant is credentials" in text
        assert "Decide everything else yourself" in text

    def test_owner_stop_limits_follow_the_recorder_constants_on_every_phase(self) -> None:
        for phase in ("coding", "reviewing", "planning"):
            text = "\n".join(envelope_contract_lines(phase)).casefold()
            assert f"1-{question_card.MAX_CHECKED_FACTS} facts" in text
            assert f"question max {question_card.MAX_QUESTION_CHARS} chars" in text
            assert f"max {question_card.MAX_SENTENCE_CHARS} chars, one line" in text
            assert f"max {question_card.MAX_WORDS} words total" in text
            assert f"labels unique/nonblank, max {question_card.MAX_LABEL_CHARS} chars" in text
            assert f"consequences max {question_card.MAX_CONSEQUENCE_CHARS} chars" in text
            assert f"0 or {question_card.MIN_OPTIONS}-{question_card.MAX_OPTIONS}" in text
            assert "exactly one recommended first" in text
            assert "no ids, paths, flags or code names" in text

    def test_each_owner_stop_limit_is_read_at_render_time(self) -> None:
        for name, fragment in _SENTINEL_FRAGMENTS.items():
            for phase in ("coding", "shipping", "reviewing"):
                with self.subTest(name=name, phase=phase), patch.object(question_card, name, 7):
                    assert fragment in "\n".join(envelope_contract_lines(phase)).casefold()

    def test_every_question_card_limit_is_stated_or_exempt(self) -> None:
        limits = {name for name in vars(question_card) if re.fullmatch(r"(?:MAX|MIN)_[A-Z_]+", name)}
        not_agent_authored = {"MAX_CARD_TITLE_CHARS"}
        assert limits - not_agent_authored == set(_SENTINEL_FRAGMENTS)

    def test_final_output_reminder_names_the_phase_evidence_key(self) -> None:
        line = final_output_reminder_line(_WORK_PHASE)
        assert "`files_modified`" in line
        assert "refused" in line

    def test_final_output_reminder_on_a_phase_with_no_evidence_key(self) -> None:
        assert "`summary`" in final_output_reminder_line("scoping")

    def test_example_satisfies_the_phase_evidence_gate(self) -> None:
        # A brief whose example would itself be refused teaches the wrong shape.
        for phase in ("coding", "reviewing", "testing", "planning", "shipping", "answering", "scanning_news"):
            assert check_evidence(envelope_example(phase), phase) == "", phase

    def test_review_example_and_schema_include_criterion_text_hash(self) -> None:
        example = envelope_example("reviewing")
        assert "text_hash" in example["review_verdict"]["rubric_grades"][0]
        properties = RESULT_JSON_SCHEMA["properties"]
        assert isinstance(properties, dict)
        review = properties["review_verdict"]
        assert isinstance(review, dict)
        review_properties = review["properties"]
        assert isinstance(review_properties, dict)
        grades = review_properties["rubric_grades"]
        assert isinstance(grades, dict)
        items = grades["items"]
        assert isinstance(items, dict)
        item_properties = items["properties"]
        assert isinstance(item_properties, dict)
        assert "text_hash" in item_properties

    def test_example_uses_only_schema_declared_keys(self) -> None:
        for phase in ("coding", "reviewing", "shipping"):
            assert set(envelope_example(phase)) <= set(allowed_keys()), phase

    def test_sweep_example_requires_the_window_urls(self) -> None:
        text = "\n".join(envelope_contract_lines("backlog_sweep", read_only_venue=True))
        example = _contract_example(text)
        proposals = example["sweep_proposals"]

        assert isinstance(proposals, dict)
        assert proposals["examined"] == ["<every issue URL in ISSUE WINDOW>"]
        assert "replace the placeholder" in text


class TestReviewVerdictMustDiscloseTheHeadItBoundTo(SimpleTestCase):
    """#4168: the head is the one field the merge-safety chain cannot infer."""

    def _required_properties(self) -> list[str]:
        properties = RESULT_JSON_SCHEMA["properties"]
        assert isinstance(properties, dict)
        schema = properties["review_verdict"]
        assert isinstance(schema, dict)
        required = schema["required"]
        assert isinstance(required, list)
        return [str(name) for name in required]

    def _example_reviewed_sha(self) -> str:
        verdict = envelope_example("reviewing").get("review_verdict")
        assert isinstance(verdict, dict)
        asserted_head = verdict.get("reviewed_sha")
        assert isinstance(asserted_head, str)
        return asserted_head

    def test_reviewed_sha_is_a_required_property(self) -> None:
        assert "reviewed_sha" in self._required_properties()

    def test_the_copyable_example_shows_a_placeholder_not_a_literal_sha(self) -> None:
        # A verbatim copy of a literal SHA is indistinguishable from a real assertion —
        # the undisclosed-head miss wearing a disclosed head's clothes.
        asserted_head = self._example_reviewed_sha()
        assert asserted_head.startswith("<")
        assert asserted_head.endswith(">")


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


class TestVerdictPhasesAreTaughtTheChecksRule(SimpleTestCase):
    """#4522: cut the merge_safe-over-red-checks contradiction where it is written.

    ``ReviewVerdict.record`` refuses that combination and the refusal is right, but no
    re-review of the same head can satisfy it — the checks stay red — so the brief has to
    tell the reviewer the recordable shape (a HOLD carrying the CI finding) BEFORE it
    writes the contradiction. Prompt-side and unfalsifiable on its own; these only pin
    that the clause reaches the phases that can breach it and no others.
    """

    def test_a_verdict_returning_phase_carries_the_rule(self) -> None:
        for phase in ("reviewing", "codex_reviewing", "codex_adversarial_reviewing"):
            text = "\n".join(envelope_contract_lines(phase))
            assert VERDICT_CHECKS_RULE in text, phase

    def test_a_phase_that_returns_no_verdict_is_not_taught_a_reviewer_rule(self) -> None:
        for phase in ("coding", "planning", "shipping", "scoping"):
            text = "\n".join(envelope_contract_lines(phase))
            assert VERDICT_CHECKS_RULE not in text, phase

    def test_the_rule_names_the_recordable_shape_not_only_the_refusal(self) -> None:
        # A brief that only forbids leaves the reviewer without an answer for a red PR,
        # which is how "merge_safe anyway" gets written in the first place.
        assert '"hold"' in VERDICT_CHECKS_RULE
        assert '"findings"' in VERDICT_CHECKS_RULE

    def test_the_clause_does_not_disturb_the_copyable_example(self) -> None:
        # The copyable example remains decodable after the contract's prose clauses.
        text = "\n".join(envelope_contract_lines("reviewing"))
        assert check_evidence(_contract_example(text), "reviewing") == ""
