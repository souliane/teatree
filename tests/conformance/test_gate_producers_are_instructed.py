"""Every evidence producer is named in a skill, including unconditional gates."""

import re
from datetime import date

import pytest

from teatree.config.gate_evidence import GATE_EVIDENCE, ActivationIntent, GateEvidence, ObservableKind
from tests.conformance._src_tree import REPO_ROOT

_SKILLS = REPO_ROOT / "skills"

#: A `t3 …` invocation inside the satisfier prose, minus the overlay placeholder — the token an
#: agent would actually type, and the one a skill has to carry for the agent to type it.
_COMMAND = re.compile(r"`t3 (?:<overlay> )?([a-z][a-z-]*(?: [a-z][a-z-]*)*)`")

_FIXTURE_ENTRY = GateEvidence(
    setting="fixture_gate_enabled",
    off_value=False,
    kind=ObservableKind.MODEL,
    target="core.Ticket",
    shipped=date(2026, 1, 1),
    intent=ActivationIntent.UNDECIDED,
    rationale="fixture",
    satisfier="`t3 <overlay> ticket plan`",
)

_ALWAYS_ON_PRODUCERS = (
    "repro record-red",
    "repro record-green",
    "repro waive",
    "review record-evidence",
    "lifecycle record-review-context",
    "lifecycle record-anti-vacuity",
    "review record --ticket-id",
    "ticket rubric-set",
)


def _producers(entry: GateEvidence) -> set[str]:
    return set(_COMMAND.findall(entry.satisfier))


def _instructed(command: str) -> bool:
    """Does any skill file tell an agent to run *command*?"""
    return any(command in path.read_text(encoding="utf-8") for path in _SKILLS.rglob("*.md"))


_OBSERVABLE_GATES = [
    entry for entry in GATE_EVIDENCE.values() if entry.kind is not ObservableKind.NONE and _producers(entry)
] + [_FIXTURE_ENTRY]


class TestTheProbeFindsWhatIsThere:
    """The control. Without it an empty-skills-dir bug would satisfy every assertion below."""

    def test_a_command_known_to_be_instructed_is_found(self) -> None:
        assert _instructed("ticket plan")

    def test_a_command_no_skill_could_name_is_not_found(self) -> None:
        assert not _instructed("ticket definitely-not-a-real-command")

    @pytest.mark.parametrize("command", _ALWAYS_ON_PRODUCERS)
    def test_an_unconditional_gate_producer_is_instructed(self, command: str) -> None:
        assert _instructed(command)


class TestEveryObservableGateHasAnInstructedProducer:
    @pytest.mark.parametrize("entry", _OBSERVABLE_GATES, ids=lambda entry: entry.setting)
    def test_the_producer_is_named_in_a_skill(self, entry: GateEvidence) -> None:
        assert _producers(entry)
        uninstructed = sorted(command for command in _producers(entry) if not _instructed(command))

        assert not uninstructed, (
            f"{entry.setting} is gated on evidence nothing is instructed to produce: {uninstructed}. "
            f"The gate, the CLI and the model all exist — name the command in the skill for the "
            f"phase that should run it, or the gate can only ever be armed into blocking every ticket."
        )
