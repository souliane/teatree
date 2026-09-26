# test-path: cross-cutting
"""A loop timer is one clock per BOX, so its setting refuses an overlay scope.

The scanners those cadences feed are built with ``overlay=""`` — one global job for the
whole machine — and the machine-wide tick cadence resolves through ``cadence_seconds()``,
which reads whichever overlay happens to be active. So a per-overlay row is either
unreachable or decided by process accident, and in both cases it is an opinion the box
cannot honour.

Prevention rather than handling (E1): nothing may WRITE such a row, and a box already
carrying one is refused loudly by the migration rather than resolved past in silence.
"""

import pytest
from django.core.exceptions import ValidationError
from django.test import TestCase

from teatree.config import OVERLAY_OVERRIDABLE_SETTINGS
from teatree.config.known_settings import SETTING_ENTRIES
from teatree.config.setting_registries import BOX_GLOBAL_SETTINGS
from teatree.core.models import ConfigSetting

#: Any registered-looking overlay name; the refusal keys on the scope being non-empty.
_AN_OVERLAY = "some-overlay"


class TestTheBoxGlobalSetIsDerivedNotHandListed:
    """The set comes off the schema marker, so a new key declares itself rather than being listed."""

    def test_the_derived_set_matches_the_hand_copy(self) -> None:
        from teatree.config.schema import derive_box_global_settings  # noqa: PLC0415 — pydantic stays off cold reads

        assert derive_box_global_settings() == BOX_GLOBAL_SETTINGS

    def test_every_box_global_key_is_still_a_db_home_setting(self) -> None:
        # Box-global narrows the SCOPE, never the storage: a global row still resolves.
        assert set(OVERLAY_OVERRIDABLE_SETTINGS) >= BOX_GLOBAL_SETTINGS

    def test_the_machine_wide_tick_cadence_is_box_global(self) -> None:
        assert "loop_cadence_seconds" in BOX_GLOBAL_SETTINGS

    def test_a_per_overlay_scanner_cadence_stays_scoped(self) -> None:
        # The control: `scanner_factories` fans per overlay and resolves
        # `get_effective_settings(overlay_name)`, so its cadence IS honourable per overlay.
        # A blanket sweep of every `*_cadence_*` key would take it.
        assert "architectural_review_cadence_hours" not in BOX_GLOBAL_SETTINGS


class TestTheWriteSeamRefusesAnOverlayScope(TestCase):
    """The only entry point for such a row is the write seam, so that is where it is closed."""

    def test_an_overlay_scoped_write_is_refused(self) -> None:
        with pytest.raises(ValueError, match="one clock per box"):
            ConfigSetting.objects.set_value("loop_cadence_seconds", 300, scope=_AN_OVERLAY)

    def test_a_global_write_still_lands(self) -> None:
        ConfigSetting.objects.set_value("loop_cadence_seconds", 300)
        assert ConfigSetting.objects.get_effective("loop_cadence_seconds") == 300

    def test_an_ordinary_key_keeps_its_overlay_scope(self) -> None:
        ConfigSetting.objects.set_value("architectural_review_cadence_hours", value=24, scope=_AN_OVERLAY)
        assert ConfigSetting.objects.filter(key="architectural_review_cadence_hours", scope=_AN_OVERLAY).exists()


class TestTheGlobalOnlySetIsDerivedNotHandListed:
    def test_the_derived_set_is_every_key_outside_the_overlay_registry(self) -> None:
        from teatree.config.schema import derive_global_only_settings  # noqa: PLC0415 — pydantic stays off cold reads

        assert derive_global_only_settings() == frozenset(SETTING_ENTRIES) - frozenset(OVERLAY_OVERRIDABLE_SETTINGS)


class TestAGlobalOnlyKeyRefusesAnOverlayScope(TestCase):
    """A cold-registry key is read at the global scope alone, so an overlay row for it is never read."""

    def test_an_overlay_scoped_write_is_refused_and_writes_nothing(self) -> None:
        with pytest.raises(ValueError, match="global scope only"):
            ConfigSetting.objects.set_value("agent_tier_models", {"cheap": "m"}, scope=_AN_OVERLAY)
        assert not ConfigSetting.objects.filter(key="agent_tier_models").exists()

    def test_a_bulk_write_carrying_one_is_refused_whole(self) -> None:
        with pytest.raises(ValueError, match="global scope only"):
            ConfigSetting.objects.set_values(
                [
                    ("mode", "auto", ""),
                    ("agent_session_model", "m", _AN_OVERLAY),
                ]
            )
        assert not ConfigSetting.objects.exists()

    def test_its_global_write_still_lands(self) -> None:
        ConfigSetting.objects.set_value("agent_tier_models", {"cheap": "m"})
        assert ConfigSetting.objects.get_effective("agent_tier_models") == {"cheap": "m"}

    def test_an_overlay_registry_agent_key_keeps_its_overlay_scope(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "claude_sdk", scope=_AN_OVERLAY)
        assert ConfigSetting.objects.get_effective("agent_harness", scope=_AN_OVERLAY) == "claude_sdk"

    def test_model_validation_refuses_it_so_the_admin_form_does(self) -> None:
        row = ConfigSetting(key="agent_tier_models", scope=_AN_OVERLAY, value={"cheap": "m"})
        with pytest.raises(ValidationError, match="global scope only"):
            row.full_clean()
