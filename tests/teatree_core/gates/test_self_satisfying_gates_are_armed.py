# test-path: cross-cutting
"""Tier 1 is "can it be VALIDATED", not "may it be turned on" — and all three are owner calls.

The rollout tiering classifies three gates as SELF-SATISFYING: they need nothing wired to
become satisfiable, because they produce the evidence they read. That is true of all three
and, measured against the tree, sufficient for none of them.

``critic_gate_mode`` and ``require_merge_quality_verdict`` are ALSO pinned safety-posture
flags, and one of the two standing invariants naming them says why in its own words: the
score metric and the critic gate stay dark and off, so intake being live never reaches a
config write or a merge. Arming either means inverting a must-stay-dark invariant.

``require_merge_evidence`` carries neither property — no flag, no safety-posture set, no
model call — and it was armed here, then reverted on the measurement. The tiering's reason
for calling it safe is that the merge keystone already writes the ``MergeAudit`` it reads,
which is true and incomplete: the BOARD-RECONCILE lane advances a ticket to MERGED from a PR
row, explicitly supports running with no forge probe, and the armed gate refuses every one of
those advances. Fourteen green tests went red on the flip. That is the effect #4375's own
body says needs checking BEFORE this gate is armed rather than after, and the check now has
an answer.

So the tier-1 population is armable-in-principle and decided-by-nobody in fact. The
inert-gate scanner keeps asking, which is the surface that ask belongs on.
"""

from teatree.config.enums import CriticGateMode
from teatree.config.feature_flags import FEATURE_FLAGS, FlagStage
from teatree.config.gate_evidence import GATE_EVIDENCE
from teatree.config.settings import UserSettings


class TestAllThreeStayOffPendingAnOwnerDecision:
    def test_the_two_critic_gates_stay_dark_and_off(self) -> None:
        defaults = UserSettings()
        assert FEATURE_FLAGS["critic_gate_mode"].stage is FlagStage.DARK
        assert defaults.critic_gate_mode is CriticGateMode.OFF
        assert FEATURE_FLAGS["require_merge_quality_verdict"].stage is FlagStage.DARK
        assert defaults.require_merge_quality_verdict is False

    def test_the_merge_evidence_gate_stays_off_and_declared(self) -> None:
        # Still declared: the registry's domain is gates that ship OFF, and this one does.
        assert UserSettings().require_merge_evidence is False
        assert "require_merge_evidence" in {entry.setting for entry in GATE_EVIDENCE.values()}

    def test_each_summary_says_arming_is_an_owner_call(self) -> None:
        # So the next reader finds the reason before the suite tells them by going red.
        for key in ("critic_gate_mode", "require_merge_quality_verdict"):
            assert "owner call" in FEATURE_FLAGS[key].summary, key


class TestTheEscapeHatchesAreIntactForWhoeverDecides:
    def test_every_tier_one_gate_stays_per_overlay_overridable(self) -> None:
        from teatree.config import OVERLAY_OVERRIDABLE_SETTINGS  # noqa: PLC0415 — deferred: registry read

        for key in ("critic_gate_mode", "require_merge_quality_verdict", "require_merge_evidence"):
            assert key in OVERLAY_OVERRIDABLE_SETTINGS, key
