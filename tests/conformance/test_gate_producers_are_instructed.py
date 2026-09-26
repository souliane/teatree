"""Every gate's evidence producer is named in a skill — the reason twelve gates went dark.

The gates are not wrong and their machinery is not missing: the CLI, the manager, the model and
the gate all exist for each one. What is missing is the SKILL, which is how an agent learns to
do anything. Measured 2026-08-27 with a working control (`ticket plan` appears in 2 of 39 skill
entries, so the probe finds what is there): five of seven evidence producers appeared in ZERO
skill files, `rubric-set` among them — 6 source hits, 2 test hits, nothing telling an agent to
run it. Nothing was ever instructed to produce the evidence, so the evidence tables stayed empty
and the gates stayed off for up to 85 days.

This is the general mechanism, not seven hand-checks. A gate added later with a producer nobody
is told to run fails here, before it can be armed and block every ticket.

Refusal-only gates (:attr:`ObservableKind.NONE`) are out of scope by construction: they write no
artifact, so there is no producer to instruct.
"""

import re

import pytest

from teatree.config.gate_evidence import GATE_EVIDENCE, GateEvidence, ObservableKind
from tests.conformance._src_tree import REPO_ROOT

_SKILLS = REPO_ROOT / "skills"

#: A `t3 …` invocation inside the satisfier prose, minus the overlay placeholder — the token an
#: agent would actually type, and the one a skill has to carry for the agent to type it.
_COMMAND = re.compile(r"`t3 (?:<overlay> )?([a-z][a-z-]*(?: [a-z][a-z-]*)*)`")

#: Gates whose satisfier is prose about a path that runs itself rather than a command an agent
#: issues. Each is self-arming: naming it in a skill would instruct nobody to do anything.
_SELF_ARMING = frozenset({"critic_gate_mode", "require_merge_quality_verdict", "send_proxy_mode"})


def _producers(entry: GateEvidence) -> set[str]:
    return set(_COMMAND.findall(entry.satisfier))


def _instructed(command: str) -> bool:
    """Does any skill file tell an agent to run *command*?"""
    return any(command in path.read_text(encoding="utf-8") for path in _SKILLS.rglob("*.md"))


_OBSERVABLE_GATES = sorted(
    entry.setting
    for entry in GATE_EVIDENCE.values()
    if entry.kind is not ObservableKind.NONE and entry.setting not in _SELF_ARMING and _producers(entry)
)


class TestTheProbeFindsWhatIsThere:
    """The control. Without it an empty-skills-dir bug would satisfy every assertion below."""

    def test_a_command_known_to_be_instructed_is_found(self) -> None:
        assert _instructed("ticket plan")

    def test_a_command_no_skill_could_name_is_not_found(self) -> None:
        assert not _instructed("ticket definitely-not-a-real-command")


class TestEveryObservableGateHasAnInstructedProducer:
    @pytest.mark.parametrize("setting", _OBSERVABLE_GATES)
    def test_the_producer_is_named_in_a_skill(self, setting: str) -> None:
        entry = GATE_EVIDENCE[setting]
        uninstructed = sorted(command for command in _producers(entry) if not _instructed(command))

        assert not uninstructed, (
            f"{setting} is gated on evidence nothing is instructed to produce: {uninstructed}. "
            f"The gate, the CLI and the model all exist — name the command in the skill for the "
            f"phase that should run it, or the gate can only ever be armed into blocking every ticket."
        )
