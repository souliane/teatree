"""The dispatched reviewer must be told what an identity needs, not only what it must not be.

The prompt used to hand the agent `"<your reviewer id, NOT a maker/loop role>"` — a purely
NEGATIVE constraint. An agent that picked `external-review-agent:task-3200` satisfied it and
was still refused fail-closed, because nothing said the identity must CARRY a recognised
reviewer role word. That lane could never record a verdict: it failed twice on #4678, tripped
the repair-halt, and escalated to the owner as questions no answer could resolve.

Nothing couples the prompt text to `REVIEWER_ROLE_COMPONENTS` at runtime — importing the model
module from `teatree.agents` would pull Django through `core.models.__init__` and trip the
intra-core deferred-import ratchet. These tests are the coupling instead: change the frozenset
without changing the prompt and they go red.
"""

from teatree.agents import envelope_contract, phase_blocks
from teatree.core.models.auto_review_dispatch import build_review_contract
from teatree.core.models.reviewer_identity import REVIEWER_ROLE_COMPONENTS, is_independent_reviewer_identity


def _prompt_surfaces() -> list[str]:
    """Every rendered surface that shows the agent a reviewer_identity slot."""
    return [
        "\n".join(phase_blocks._REVIEW_VERDICT_RETURN_LINES),
        str(envelope_contract.envelope_example("reviewing")),
        build_review_contract(slug="o/r", pr_id=1, head_sha="a" * 40, pr_url="https://github.com/o/r/pull/1"),
    ]


class TestTheHintNamesWhatIsActuallyAdmitted:
    def test_every_surface_names_at_least_one_admitting_token(self) -> None:
        for surface in _prompt_surfaces():
            assert any(token in surface for token in REVIEWER_ROLE_COMPONENTS), surface[:200]

    def test_every_admitting_token_is_offered(self) -> None:
        # Drift guard: widen or narrow REVIEWER_ROLE_COMPONENTS and this fails until the
        # prompt is updated with it. That is the whole coupling.
        for surface in _prompt_surfaces():
            missing = sorted(t for t in REVIEWER_ROLE_COMPONENTS if t not in surface)
            assert not missing, f"prompt omits admitting tokens {missing}"

    def test_an_identity_built_from_the_hint_is_actually_admitted(self) -> None:
        # The end-to-end property: an agent following the instruction produces a
        # recordable identity. This is what the old negative-only wording did not give.
        for token in REVIEWER_ROLE_COMPONENTS:
            assert is_independent_reviewer_identity(f"external-{token}-agent:task-3200")

    def test_the_identity_the_lane_actually_produced_is_still_refused(self) -> None:
        # The anti-vacuity control: the gate itself is unchanged, so the bad identity
        # must still be refused. If this ever passes, the fix went in the wrong place.
        assert not is_independent_reviewer_identity("external-review-agent:task-3200")
