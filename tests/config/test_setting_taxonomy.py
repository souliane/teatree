# test-path: cross-cutting
"""The one classifier every per-key decision reads (B11).

Five registries answered "what kind of key is this" separately, and every decision that
needed the answer — ask-vs-delete, the scalar split, the inert-gate arm — re-derived it by
hand. These are the fitness functions for the single view over them.

The equivalence assertions are the load-bearing ones: the taxonomy is a VIEW, so each class
must return exactly what its registry returns today. A view that quietly dropped a registry
would still be total, still be self-consistent, and would silently reclassify every key that
registry owned — which is why each class is also asserted NON-EMPTY before it is compared.
"""

import pytest

from teatree.config.feature_flags import FEATURE_FLAGS
from teatree.config.gate_evidence import GATE_EVIDENCE
from teatree.config.known_settings import ALL_KNOWN_CONFIG_SETTINGS
from teatree.config.registries import COLD_HOOK_SETTINGS, COLD_SETTINGS, REGISTRY_KEYS
from teatree.config.retired_settings import RETIRED_SETTINGS
from teatree.config.setting_registries import SAFETY_POSTURE_KEYS
from teatree.config.setting_taxonomy import (
    SettingClass,
    UnclassifiedSettingError,
    classify,
    governance_trailer,
    is_gate_switch,
    owner_only_reason,
    taxonomy,
)

_REGISTRY_BY_CLASS: dict[SettingClass, set[str]] = {
    SettingClass.FEATURE_FLAG: set(FEATURE_FLAGS),
    SettingClass.GATE: set(GATE_EVIDENCE),
    SettingClass.GATE_SWITCH: {
        key for key in {*ALL_KNOWN_CONFIG_SETTINGS, *(r.key for r in RETIRED_SETTINGS)} if is_gate_switch(key)
    },
    SettingClass.SAFETY_POSTURE: set(SAFETY_POSTURE_KEYS),
    SettingClass.COLD: set(COLD_SETTINGS) | set(COLD_HOOK_SETTINGS),
    SettingClass.REGISTRY: set(REGISTRY_KEYS),
    SettingClass.RETIRED: {entry.key for entry in RETIRED_SETTINGS},
}


def _keys_classified(kind: SettingClass) -> set[str]:
    return {key for key, taxon in taxonomy().items() if kind in taxon.classes}


class TestTheDomainIsEveryKeyThatEXISTS:
    def test_every_live_key_has_a_taxon(self) -> None:
        assert set(ALL_KNOWN_CONFIG_SETTINGS) <= set(taxonomy())

    def test_a_retired_key_is_classified_though_it_is_no_longer_a_live_key(self) -> None:
        """A view over the live union alone answers "unknown" for the keys whose job is to answer."""
        retired = _REGISTRY_BY_CLASS[SettingClass.RETIRED]
        assert retired
        assert retired.isdisjoint(ALL_KNOWN_CONFIG_SETTINGS)
        assert retired <= set(taxonomy())

    def test_a_key_belonging_to_neither_domain_is_refused_loudly(self) -> None:
        with pytest.raises(UnclassifiedSettingError, match="no_such_setting_key"):
            classify("no_such_setting_key")


class TestEachClassEqualsItsRegistry:
    @pytest.mark.parametrize("kind", list(_REGISTRY_BY_CLASS))
    def test_the_view_returns_what_the_registry_returns(self, kind: SettingClass) -> None:
        expected = _REGISTRY_BY_CLASS[kind]
        assert expected, f"{kind} is empty — the comparison below would hold over nothing"
        assert _keys_classified(kind) == expected

    def test_a_key_may_hold_several_classes_at_once(self) -> None:
        """Ten keys are both a gate and a feature flag, so a single-class answer would be a lie."""
        both = _REGISTRY_BY_CLASS[SettingClass.GATE] & _REGISTRY_BY_CLASS[SettingClass.FEATURE_FLAG]
        assert both
        for key in both:
            assert {SettingClass.GATE, SettingClass.FEATURE_FLAG} <= classify(key).classes


class TestPlainIsALeftoverNeverADeclaration:
    def test_plain_never_shares_a_key_with_a_registry_class(self) -> None:
        for key, taxon in taxonomy().items():
            assert (taxon.classes == {SettingClass.PLAIN}) == (SettingClass.PLAIN in taxon.classes), key

    def test_plain_is_exactly_what_no_registry_claims(self) -> None:
        claimed = set().union(*_REGISTRY_BY_CLASS.values())
        assert _keys_classified(SettingClass.PLAIN) == set(taxonomy()) - claimed

    def test_no_gate_switch_reads_as_an_ordinary_tunable(self) -> None:
        """A switch that arms a safety gate must never reach an audit labelled PLAIN.

        `GATE_EVIDENCE` models what proves a gate RAN, and a switch has no such artifact, so
        six of them held no class at all — an audit reading this view saw six safety switches
        as ordinary tunables, which is the deletion pile.
        """
        switches = {key for key in taxonomy() if is_gate_switch(key)}
        assert len(switches) > 5, "a shrunken switch set would make the assertion below trivially true"

        plain = sorted(key for key in switches if classify(key).classes == {SettingClass.PLAIN})

        assert plain == []

    def test_the_write_predicate_and_the_view_agree_on_every_gate_switch(self) -> None:
        # The underlying bug was TWO notions of "is this a gate": `owner_only_reason` globbed
        # the names itself while `classify` knew nothing about them.
        for key in taxonomy():
            assert is_gate_switch(key) <= bool(owner_only_reason(key)), key
            assert is_gate_switch(key) == (SettingClass.GATE_SWITCH in classify(key).classes), key


class TestTheGovernanceTrailer:
    def test_a_gate_that_is_also_a_flag_names_both(self) -> None:
        trailer = governance_trailer("critic_gate_mode")
        assert "gate" in trailer
        assert "feature-flag" in trailer
        assert "stage=" in trailer

    def test_a_safety_posture_key_is_announced(self) -> None:
        assert "safety-posture" in governance_trailer(next(iter(SAFETY_POSTURE_KEYS)))

    def test_a_plain_setting_carries_no_trailer(self) -> None:
        assert governance_trailer("issue_implementer_max_concurrent") == ""

    def test_an_unknown_key_carries_no_trailer_rather_than_raising(self) -> None:
        """The trailer decorates an already-resolved value, so an unplaceable key must not crash it."""
        assert governance_trailer("no_such_setting_key") == ""


class TestOwnerOnlyIsOneAnswerForEverySurface:
    """The predicate the MCP tool and the write chokepoint both read (B11 + commit 11)."""

    def test_every_governed_class_is_owner_only(self) -> None:
        governed = {
            SettingClass.FEATURE_FLAG,
            SettingClass.GATE,
            SettingClass.GATE_SWITCH,
            SettingClass.SAFETY_POSTURE,
            SettingClass.COLD,
            SettingClass.REGISTRY,
        }
        unguarded = [
            key for key, taxon in taxonomy().items() if taxon.classes & governed and not owner_only_reason(key)
        ]
        assert unguarded == []

    def test_a_plain_setting_is_writable(self) -> None:
        assert owner_only_reason("issue_implementer_max_concurrent") == ""

    def test_a_safety_posture_write_is_named_an_authorization(self) -> None:
        assert "authorization" in owner_only_reason("substrate_auto_merge_authorized_by")

    def test_a_gate_switch_with_no_evidence_entry_is_owner_only(self) -> None:
        assert SettingClass.GATE not in taxonomy()["orchestrator_bash_gate_enabled"].classes
        assert SettingClass.GATE_SWITCH in taxonomy()["orchestrator_bash_gate_enabled"].classes
        assert owner_only_reason("orchestrator_bash_gate_enabled")

    def test_a_registry_row_is_a_declared_class_not_a_hand_lane(self) -> None:
        assert taxonomy()["overlays"].classes == frozenset({SettingClass.REGISTRY})
        assert owner_only_reason("overlays")

    def test_an_unknown_key_is_writable_rather_than_a_crash(self) -> None:
        assert owner_only_reason("no_such_setting_key") == ""


class TestCredentialPassKeysAreOwnerOnly:
    def test_repointing_the_secret_a_credential_reads_is_never_unattended(self) -> None:
        assert "credential" in owner_only_reason("notion_token_pass_key")
