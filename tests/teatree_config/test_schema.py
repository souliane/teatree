"""Choice derivation over the settings schema (:func:`setting_choices`).

A key whose admissible values are a CLOSED set must derive them, so the dashboard
renders a select and an invalid value is impossible to PICK rather than merely
rejected after the fact. A key whose value is a registry NAME resolved elsewhere
must derive nothing. Both directions are pinned because only the second is silent:
a wrongly closed set reads as a tidier UI right up until an overlay's registered
name is no longer selectable.
"""

import pytest

from teatree.config.schema import setting_choices

#: Settings naming a SKILL — resolved through the skill registry, so any name the
#: operator has installed is admissible and no member list can be complete.
SKILL_NAME_SETTINGS = (
    "architectural_review_skill",
    "backlog_sweep_skill",
    "dogfood_smoke_skill",
    "eval_local_skill",
    "review_skill",
    "scanning_news_skill",
)


class TestClosedValueSetsDeriveTheirMembers:
    def test_repo_mode_offers_auto_detect_beside_both_pinned_verdicts(self) -> None:
        assert setting_choices("repo_mode") == ("", "solo", "collaborative")

    def test_privacy_offers_unset_beside_both_strictness_levels(self) -> None:
        assert setting_choices("privacy") == ("", "strict", "relaxed")


class TestRegistryNamesStayOpen:
    def test_agent_harness_derives_nothing_so_a_registered_transport_stays_selectable(self) -> None:
        assert setting_choices("agent_harness") == ()

    @pytest.mark.parametrize("key", SKILL_NAME_SETTINGS)
    def test_a_skill_name_derives_nothing(self, key: str) -> None:
        assert setting_choices(key) == ()
