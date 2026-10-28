"""The shared write-path validator every config write surface routes through."""

import pytest

from teatree.config.setting_registries import BOX_GLOBAL_SETTINGS
from teatree.config.write_validation import ConfigWriteError, validate_config_write


class TestValidateConfigWrite:
    def test_returns_the_canonical_coerced_value(self) -> None:
        # An upper-case enum canonicalises, a numeric string coerces to int (#258).
        assert validate_config_write("mode", "AUTO") == "auto"
        assert validate_config_write("issue_implementer_max_concurrent", "5") == 5

    def test_real_bool_round_trips(self) -> None:
        assert validate_config_write("autoload", raw=True) is True

    def test_quoted_bool_string_is_rejected(self) -> None:
        # bool("false") == True would silently enable an opt-in setting — must raise.
        with pytest.raises(ConfigWriteError):
            validate_config_write("autoload", "false")

    def test_scalar_for_a_list_setting_is_rejected(self) -> None:
        with pytest.raises(ConfigWriteError):
            validate_config_write("excluded_skills", raw=True)

    def test_error_message_carries_the_parser_reason(self) -> None:
        with pytest.raises(ConfigWriteError, match="bool"):
            validate_config_write("autoload", 1)

    def test_config_write_error_is_a_value_error(self) -> None:
        assert issubclass(ConfigWriteError, ValueError)

    def test_unknown_key_is_refused_by_the_shared_write_seam(self) -> None:
        with pytest.raises(ConfigWriteError, match="unknown config key: unregistered_setting"):
            validate_config_write("unregistered_setting", 1)

    def test_retired_key_explains_why_it_has_no_setting(self) -> None:
        with pytest.raises(ConfigWriteError, match=r"branch_prefix.*retired.*no readers"):
            validate_config_write("branch_prefix", "ac")

    @pytest.mark.parametrize("raw", [4, 0, 0.1, "nan", "inf"])
    def test_a_per_core_factor_outside_its_range_is_refused(self, raw: object) -> None:
        with pytest.raises(ConfigWriteError, match=r"between 0\.25 and 2"):
            validate_config_write("admission_write_concurrency_per_core", raw)

    @pytest.mark.parametrize(("raw", "kept"), [(0.25, 0.25), (1, 1.0), ("2.0", 2.0)])
    def test_a_per_core_factor_inside_its_range_is_kept(self, raw: object, kept: float) -> None:
        assert validate_config_write("admission_write_concurrency_per_core", raw) == pytest.approx(kept)

    @pytest.mark.parametrize(
        ("key", "valid", "invalid"),
        [
            ("admission_load_brake_per_core", 2.0, 0.0),
            ("admission_load_resume_per_core", 1.5, 7.0),
        ],
    )
    def test_load_factors_are_validated_on_write(self, key: str, valid: float, invalid: float) -> None:
        assert validate_config_write(key, valid) == pytest.approx(valid)
        with pytest.raises(ConfigWriteError, match="between"):
            validate_config_write(key, invalid)

    @pytest.mark.parametrize(("raw", "kept"), [(1, 1), ("4", 4), (16, 16)])
    def test_a_forge_pool_cap_inside_its_range_is_kept(self, raw: object, kept: int) -> None:
        assert validate_config_write("forge_pool_max_connections", raw) == kept

    @pytest.mark.parametrize("invalid", [0, 17, -3])
    def test_a_forge_pool_cap_outside_its_range_is_refused(self, invalid: int) -> None:
        with pytest.raises(ConfigWriteError, match="between"):
            validate_config_write("forge_pool_max_connections", invalid)

    def test_the_forge_pool_cap_cannot_be_overridden_per_overlay(self) -> None:
        assert "forge_pool_max_connections" in BOX_GLOBAL_SETTINGS

    def test_load_factors_cannot_be_overridden_per_overlay(self) -> None:
        assert {"admission_load_brake_per_core", "admission_load_resume_per_core"} <= BOX_GLOBAL_SETTINGS
