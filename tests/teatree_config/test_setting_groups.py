"""Every config key resolves to exactly one declaration group — the settings page's grouping.

The defect this guards: the retired ``/dash/config`` band classifier returned ``""`` for
130 of 184 ``UserSettings`` fields and dropped each one from the page. A grouping whose
membership is DERIVED from the declaration bases and the registries cannot do that — but
only a total, exhaustive assertion proves it.
"""

import pytest

from teatree.config.schema import TeatreeSettingsSchema
from teatree.config.setting_groups import UNGROUPED_LABEL, group_labels, setting_group


class TestEveryKeyIsGrouped:
    def test_every_schema_key_resolves_to_a_group(self) -> None:
        ungrouped = [key for key in TeatreeSettingsSchema.model_fields if not setting_group(key)]
        assert not ungrouped, f"{len(ungrouped)} schema key(s) resolve to no group: {sorted(ungrouped)}"

    def test_no_schema_key_falls_back_to_the_ungrouped_bucket(self) -> None:
        # The bucket exists as the never-vanish guarantee, not as a resting place.
        stragglers = [key for key in TeatreeSettingsSchema.model_fields if setting_group(key) == UNGROUPED_LABEL]
        assert not stragglers, f"{len(stragglers)} key(s) landed in the catch-all: {sorted(stragglers)}"

    def test_every_group_label_is_reachable_from_some_key(self) -> None:
        used = {setting_group(key) for key in TeatreeSettingsSchema.model_fields}
        dead = [label for label in group_labels() if label not in used and label != UNGROUPED_LABEL]
        assert not dead, f"group label(s) no key belongs to: {dead}"

    def test_the_ungrouped_bucket_is_last_so_it_reads_as_the_leftovers(self) -> None:
        assert group_labels()[-1] == UNGROUPED_LABEL


class TestGroupingIsDerivedNotHandKept:
    @pytest.mark.parametrize(
        ("key", "label"),
        [
            ("autoload", "Workspace & engagement"),
            ("mode", "Mode, harness & agent runtime"),
            ("loop_cadence_seconds", "Loop cadence & throughput"),
            ("on_behalf_post_mode", "Posting on your behalf"),
            ("require_merge_evidence", "Quality gates"),
            ("provision_max_concurrency", "Provisioning"),
            ("banned_terms", "Term scanning, agent tables & cold reads"),
            ("skill_loading_gate_enabled", "Pre-Django hook gates"),
            ("overlays", "Definition registries"),
        ],
    )
    def test_a_representative_key_lands_in_its_declaring_group(self, key: str, label: str) -> None:
        assert setting_group(key) == label

    def test_a_key_no_group_declares_falls_back_visibly_rather_than_vanishing(self) -> None:
        # The 70%-dropped defect in one assertion: an unknown key gets a bucket, not silence.
        assert setting_group("a_key_no_declaration_base_carries") == UNGROUPED_LABEL
