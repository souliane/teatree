"""The per-phase cost taxonomy the admission governor brakes against (#4098).

The headless drain applied ONE governor verdict to the whole queue, so a 3-minute
read-only ``reviewing`` task was refused on the same brake as a 272-turn ``coding``
agent — and the phases that DRAIN the box were starved by the phases that filled it.
These pin the classification that verdict is now resolved against: what is cheap,
what is not, and — the safety property — that anything unrecognised is EXPENSIVE.
"""

from teatree.core.modelkit.phases import CHEAP_PHASES, KNOWN_PHASES, PhaseCost, cheap_phase_spellings, phase_cost

#: Write-heavy long-running phases that must NEVER inherit the exemption — the class
#: that CAUSED the measured load, listed explicitly so a future edit to the cheap set
#: cannot quietly move one of them across.
_MUST_STAY_EXPENSIVE = ("coding", "testing", "debugging", "e2e", "planning", "retro", "bughunt")


class TestPhaseCost:
    def test_reviewing_is_cheap(self) -> None:
        assert phase_cost("reviewing") is PhaseCost.CHEAP

    def test_a_short_verb_spelling_resolves_the_same_as_the_gerund(self) -> None:
        # ``Task.phase`` legitimately stores either spelling; the canonical form is the
        # key, so ``review`` must not fall through to the fail-safe EXPENSIVE default.
        assert phase_cost("review") is PhaseCost.CHEAP
        assert phase_cost("  REVIEW  ") is PhaseCost.CHEAP

    def test_coding_is_expensive(self) -> None:
        assert phase_cost("coding") is PhaseCost.EXPENSIVE
        assert phase_cost("code") is PhaseCost.EXPENSIVE

    def test_an_unknown_phase_is_expensive(self) -> None:
        # Fail-safe: an unregistered phase must never be handed the cheap-lane
        # exemption on the strength of not being recognised.
        assert phase_cost("banana") is PhaseCost.EXPENSIVE
        assert phase_cost("") is PhaseCost.EXPENSIVE

    def test_every_write_heavy_phase_stays_expensive(self) -> None:
        leaked = [phase for phase in _MUST_STAY_EXPENSIVE if phase_cost(phase) is PhaseCost.CHEAP]
        assert not leaked, f"write-heavy phase(s) moved into the cheap exemption lane: {leaked}"

    def test_every_cheap_phase_is_a_real_registered_phase(self) -> None:
        # A typo in the cheap set would be a silently dead entry — the phase would
        # never match and would be braked as EXPENSIVE with nothing reporting it.
        assert CHEAP_PHASES <= KNOWN_PHASES, sorted(CHEAP_PHASES - KNOWN_PHASES)

    def test_the_cheap_set_is_not_empty(self) -> None:
        assert CHEAP_PHASES

    def test_cheap_spellings_cover_every_alias_of_every_cheap_phase(self) -> None:
        # The DB filter matches on stored spellings, so a cheap phase stored under its
        # short verb must still be counted against the cheap lane's own bound.
        spellings = cheap_phase_spellings()
        assert "reviewing" in spellings
        assert "review" in spellings
        assert "coding" not in spellings
        assert all(phase_cost(spelling) is PhaseCost.CHEAP for spelling in spellings)
