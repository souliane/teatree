"""The shared write-path validator every config write surface routes through."""

import pytest

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
