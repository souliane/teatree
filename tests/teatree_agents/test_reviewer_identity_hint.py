"""The dispatched reviewer must be told what an identity NEEDS, not only what it must not be.

Every surface used to state a purely NEGATIVE constraint — the worst being
`"<your reviewer id, NOT a maker/loop role>"`. Nothing said the identity must CARRY a
recognised reviewer role word, so an agent choosing `external-review-agent:task-3200`
satisfied the stated rule and was still refused fail-closed. Measured against the recorded
refusals: 44 of 51 named no admitting token, and a further 7 carried one but were refused
for a maker word — so BOTH clauses are load-bearing and the instruction states both.

The instruction is built from the gate's own two frozensets
(`REVIEWER_IDENTITY_INSTRUCTION`), so prompt and gate cannot drift. These tests pin the
surfaces that render it, including the markdown ones a sub-agent reads as its system
prompt — the surface class an earlier pass of this fix missed.
"""

from pathlib import Path

from teatree.agents import envelope_contract, phase_blocks
from teatree.core.models.auto_review_dispatch import build_review_contract
from teatree.core.models.reviewer_identity import (
    REVIEWER_IDENTITY_INSTRUCTION,
    REVIEWER_ROLE_COMPONENTS,
    is_independent_reviewer_identity,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: SELF-NAMING surfaces: the agent declares its OWN identity here, so a concrete copyable
#: example is right. `agents/reviewer.md` IS the reviewer sub-agent's system prompt.
_SELF_NAMING_MARKDOWN = ("agents/reviewer.md",)

#: CITING surfaces: the agent names ANOTHER actor's identity in a CLI recipe. A template
#: here is actively wrong — it overrides the real reviewer's name, which is what the
#: keystone_merge_reviewer_is_independent eval caught. They carry the constraint, not an example.
_CITING_MARKDOWN = ("skills/ship/SKILL.md", "skills/sweeping-prs/SKILL.md", "skills/e2e-review/SKILL.md")


def _rendered_surfaces() -> dict[str, str]:
    return {
        "phase_blocks": "\n".join(phase_blocks._REVIEW_VERDICT_RETURN_LINES),
        "envelope_contract": str(envelope_contract.envelope_example("reviewing")),
        "review_contract": build_review_contract(
            slug="o/r", pr_id=1, head_sha="a" * 40, pr_url="https://github.com/o/r/pull/1"
        ),
        **{path: (_REPO_ROOT / path).read_text(encoding="utf-8") for path in _SELF_NAMING_MARKDOWN},
    }


class TestEverySurfaceCarriesTheWholeInstruction:
    def test_each_surface_renders_the_shared_instruction(self) -> None:
        # Substring-matching individual tokens is vacuous — "reviewer" occurs in prose and
        # "checker" inside "maker≠checker". Only the whole assembled string discriminates.
        for name, surface in _rendered_surfaces().items():
            assert REVIEWER_IDENTITY_INSTRUCTION in surface, name

    def test_the_instruction_states_both_constraints(self) -> None:
        # Drift guard: widen or narrow either frozenset and this fails until the gate's
        # own derivation is re-read. The positive alone left 7 recorded refusals unaddressed.
        for token in REVIEWER_ROLE_COMPONENTS:
            assert token in REVIEWER_IDENTITY_INSTRUCTION, f"admitting token {token} not offered"
        for maker in ("maker", "coding", "loop"):
            assert maker in REVIEWER_IDENTITY_INSTRUCTION, f"maker word {maker} not forbidden"

    def test_the_example_it_tells_the_agent_to_copy_is_itself_admitted(self) -> None:
        # The end-to-end property: an agent copying the example produces a RECORDABLE
        # identity. A concrete example is used over a bare token because the per-head
        # unique index would otherwise collapse two independent reviewers into one row.
        example = REVIEWER_IDENTITY_INSTRUCTION.split(" (")[0].replace("<pr-or-task-id>", "4680")
        assert is_independent_reviewer_identity(example), example

    def test_the_identity_this_lane_actually_produced_is_still_refused(self) -> None:
        # NOT an anti-vacuity control — the gate is untouched, so this passes with or
        # without the fix. It is a gate anti-REGRESSION control: if it ever goes green
        # for the wrong reason, the fix was applied to the gate instead of the prompt.
        assert not is_independent_reviewer_identity("external-review-agent:task-3200")


class TestCitingSurfacesConstrainWithoutOverridingTheRealName:
    """A CLI recipe cites ANOTHER actor, so it must not hand over a ready-made identity.

    Supplying `cold-reviewer-<id>` there made the agent emit a generic name instead of the
    reviewer actually named in its task — caught by `keystone_merge_reviewer_is_independent`,
    whose prompt names `codex` and expects that to be cited.
    """

    def test_they_carry_both_constraints(self) -> None:
        for path in _CITING_MARKDOWN:
            text = (_REPO_ROOT / path).read_text(encoding="utf-8")
            assert all(t in text for t in REVIEWER_ROLE_COMPONENTS), path
            assert "never coding/loop/maker" in text, path

    def test_they_do_not_hand_over_a_ready_made_identity(self) -> None:
        for path in _CITING_MARKDOWN:
            text = (_REPO_ROOT / path).read_text(encoding="utf-8")
            assert "cold-reviewer-<pr-or-task-id>" not in text, f"{path} templates an identity it should cite"
