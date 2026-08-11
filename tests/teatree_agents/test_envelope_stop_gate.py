"""The Stop gate that refuses a headless turn-end carrying no result envelope.

The failure it is the net under: a dispatched tester ran the suite, backgrounded
it, and ended its turn expecting a wakeup that a one-shot run never gets. The
work was real and none of it was recorded. Refusing the turn-end keeps the run —
and its context — alive for one more turn instead of discarding it.

Half of these are about the gate NOT firing: it arms only on the phases that owe
an envelope, refuses a bounded number of times, honours another Stop hook's
block, and lets the turn end on any transcript it cannot read. A gate that can
wedge a dispatch would be worse than the failure it guards.
"""

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any, cast

from claude_agent_sdk.types import HookContext, StopHookInput
from django.test import TestCase

from teatree.agents._runner_options import _build_options, resolve_envelope_stop_refusals
from teatree.agents.envelope_stop_gate import DEFAULT_ENVELOPE_STOP_REFUSALS, EnvelopeStopGate, envelope_stop_hooks
from teatree.core.models import ConfigSetting, Session, Task, Ticket

_PARKED_PROSE = (
    "All 60 targeted tests pass. Still running the broader sweep. "
    "Ending this turn - the scheduled wakeup will re-invoke me once the wait elapses."
)
_ENVELOPE = json.dumps({"summary": "ran the suite", "tests_run": [{"name": "tests/x.py::test_y", "passed": True}]})


def _assistant_entry(text: str) -> str:
    return json.dumps(
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}
    )


def _stop_input(transcript_path: str, *, stop_hook_active: bool = False) -> StopHookInput:
    payload: dict[str, Any] = {
        "hook_event_name": "Stop",
        "session_id": "s1",
        "transcript_path": transcript_path,
        "cwd": ".",
        "stop_hook_active": stop_hook_active,
    }
    return cast("StopHookInput", payload)


def _run(gate: EnvelopeStopGate, stop_input: StopHookInput) -> dict[str, Any]:
    return cast("dict[str, Any]", asyncio.run(gate.stop(stop_input, None, cast("HookContext", object()))))


class _GateTest(TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def transcript(self, *assistant_texts: str) -> str:
        lines = [json.dumps({"type": "system", "subtype": "init", "session_id": "s1"})]
        lines += [_assistant_entry(text) for text in assistant_texts]
        path = self.tmp / "transcript.jsonl"
        path.write_text("\n".join(lines), encoding="utf-8")
        return str(path)


class TestRefusesAnEnvelopeLessTurnEnd(_GateTest):
    def test_the_parked_mid_wait_turn_is_refused(self) -> None:
        gate = EnvelopeStopGate("testing")

        verdict = _run(gate, _stop_input(self.transcript(_PARKED_PROSE)))

        assert verdict["decision"] == "block"
        assert gate.refused == 1

    def test_the_refusal_names_the_one_shot_lifetime_and_the_phase_keys(self) -> None:
        # A refusal the agent cannot act on is just a stalled turn: it has to say
        # what is missing AND why waiting for a later turn will not work.
        gate = EnvelopeStopGate("testing")

        reason = _run(gate, _stop_input(self.transcript(_PARKED_PROSE)))["reason"]

        assert "one-shot" in reason.lower()
        assert "no scheduled wakeup" in reason.lower()
        assert "tests_run" in reason

    def test_an_envelope_missing_its_evidence_key_is_refused_naming_the_key(self) -> None:
        gate = EnvelopeStopGate("testing")

        verdict = _run(gate, _stop_input(self.transcript(json.dumps({"summary": "ran them, honest"}))))

        assert verdict["decision"] == "block"
        assert "tests_run" in verdict["reason"]

    def test_an_evidence_free_phase_still_owes_an_envelope(self) -> None:
        # `shipping` has no PHASE_REQUIRED_EVIDENCE entry, so nothing but the
        # recorder's own refusal would ever catch its prose-only run.
        gate = EnvelopeStopGate("shipping")

        assert gate.enabled is True
        assert _run(gate, _stop_input(self.transcript(_PARKED_PROSE)))["decision"] == "block"


class TestAllowsATurnTheRecorderWouldAccept(_GateTest):
    def test_a_run_ending_on_the_envelope_is_allowed(self) -> None:
        gate = EnvelopeStopGate("testing")

        verdict = _run(gate, _stop_input(self.transcript("Running the suite now.", _ENVELOPE)))

        assert verdict == {}
        assert gate.refused == 0

    def test_a_needs_user_input_handoff_is_allowed(self) -> None:
        # A blocked sub-agent is not claiming the phase is done, so demanding
        # phase evidence would refuse the very escalation the contract asks for.
        gate = EnvelopeStopGate("testing")
        handoff = json.dumps({"summary": "blocked", "needs_user_input": True, "user_input_reason": "no DB access"})

        assert _run(gate, _stop_input(self.transcript(handoff))) == {}

    def test_a_prose_exempt_phase_is_never_armed(self) -> None:
        gate = EnvelopeStopGate("retro")

        assert gate.enabled is False
        assert _run(gate, _stop_input(self.transcript(_PARKED_PROSE))) == {}


class TestTheGateCanNeverWedgeADispatch(_GateTest):
    def test_refusals_are_bounded_by_the_limit(self) -> None:
        gate = EnvelopeStopGate("testing", limit=2)
        stop_input = _stop_input(self.transcript(_PARKED_PROSE))

        verdicts = [_run(gate, stop_input) for _ in range(4)]

        assert [bool(verdict) for verdict in verdicts] == [True, True, False, False]
        assert gate.refused == 2

    def test_a_turn_another_stop_hook_already_blocked_is_left_alone(self) -> None:
        gate = EnvelopeStopGate("testing")

        verdict = _run(gate, _stop_input(self.transcript(_PARKED_PROSE), stop_hook_active=True))

        assert verdict == {}
        assert gate.refused == 0

    def test_a_zero_limit_disables_the_gate(self) -> None:
        gate = EnvelopeStopGate("testing", limit=0)

        assert gate.enabled is False
        assert _run(gate, _stop_input(self.transcript(_PARKED_PROSE))) == {}

    def test_an_unreadable_transcript_lets_the_turn_end(self) -> None:
        gate = EnvelopeStopGate("testing")

        assert _run(gate, _stop_input(str(self.tmp / "does-not-exist.jsonl"))) == {}
        assert _run(gate, _stop_input("")) == {}

    def test_malformed_transcript_lines_are_skipped_not_fatal(self) -> None:
        gate = EnvelopeStopGate("testing")
        path = self.tmp / "malformed.jsonl"
        path.write_text(f'not json at all\n[]\n{{"message": 3}}\n{_assistant_entry(_ENVELOPE)}\n', encoding="utf-8")

        assert _run(gate, _stop_input(str(path))) == {}

    def test_an_exploding_hook_input_lets_the_turn_end(self) -> None:
        gate = EnvelopeStopGate("testing")

        assert _run(gate, cast("StopHookInput", cast("object", "not a mapping"))) == {}


class TestArmedOnEveryDispatch(TestCase):
    def _task(self, phase: str) -> Task:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        return Task.objects.create(ticket=ticket, session=session, phase=phase)

    def test_the_bundle_registers_the_gate_on_stop(self) -> None:
        gate = EnvelopeStopGate("testing")

        hooks = envelope_stop_hooks(gate)

        assert list(hooks) == ["Stop"]
        assert hooks["Stop"][0].hooks[0] == gate.stop

    def test_a_dispatch_arms_both_the_stop_gate_and_the_spawn_ceiling(self) -> None:
        options = _build_options(self._task("testing"), "ctx", phase="testing", skills=[])

        assert options.hooks is not None
        assert set(options.hooks) == {"PreToolUse", "Stop"}

    def test_each_dispatch_gets_its_own_refusal_counter(self) -> None:
        first = _build_options(self._task("testing"), "ctx", phase="testing", skills=[])
        second = _build_options(self._task("testing"), "ctx", phase="testing", skills=[])

        assert first.hooks is not None
        assert second.hooks is not None
        assert first.hooks["Stop"][0].hooks[0] != second.hooks["Stop"][0].hooks[0]

    def test_the_shipped_limit_is_the_bounded_default(self) -> None:
        assert EnvelopeStopGate("testing").limit == DEFAULT_ENVELOPE_STOP_REFUSALS
        assert DEFAULT_ENVELOPE_STOP_REFUSALS == 2
        assert resolve_envelope_stop_refusals() == DEFAULT_ENVELOPE_STOP_REFUSALS

    def test_an_operator_row_can_disable_the_gate(self) -> None:
        ConfigSetting.objects.set_value("envelope_stop_gate_refusals", 0, scope="")

        assert resolve_envelope_stop_refusals() == 0
