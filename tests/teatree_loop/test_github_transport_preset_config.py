# test-path: cross-cutting
"""The GitHub transport preset is DB-home, fail-closed to polling (#4795).

``github_transport_preset`` selects which transport delivers GitHub events:
``polling`` (default) enables the ``github_polling`` scanner, ``webhook``
disables it in favour of native App delivery. Mirrors
``test_gitlab_approvals_config.py`` — the DB row is the sole source, default
``polling`` when unset.
"""

import pytest
from django.test import TestCase

from teatree.config import GitHubTransportPreset, get_effective_settings
from teatree.config.write_validation import ConfigWriteError, validate_config_write
from teatree.core.models import ConfigSetting


class TestGitHubTransportPresetDefault(TestCase):
    def test_polling_by_default_with_no_db_row(self) -> None:
        assert get_effective_settings().github_transport_preset is GitHubTransportPreset.POLLING

    def test_db_row_selects_webhook(self) -> None:
        ConfigSetting.objects.set_value("github_transport_preset", value="webhook")
        assert get_effective_settings().github_transport_preset is GitHubTransportPreset.WEBHOOK

    def test_db_row_back_to_polling(self) -> None:
        ConfigSetting.objects.set_value("github_transport_preset", value="webhook")
        ConfigSetting.objects.set_value("github_transport_preset", value="polling")
        assert get_effective_settings().github_transport_preset is GitHubTransportPreset.POLLING


class TestParseInvalid:
    def test_bogus_value_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid github_transport_preset"):
            GitHubTransportPreset.parse("bogus")

    def test_parse_normalises_case_and_whitespace(self) -> None:
        assert GitHubTransportPreset.parse("  WEBHOOK  ") is GitHubTransportPreset.WEBHOOK
        assert GitHubTransportPreset.parse("Polling") is GitHubTransportPreset.POLLING


class TestWriteValidation:
    def test_config_setting_set_refuses_unknown_preset(self) -> None:
        """``t3 <overlay> config_setting set`` routes every write through this coercer first."""
        with pytest.raises(ConfigWriteError, match="Invalid github_transport_preset"):
            validate_config_write("github_transport_preset", "bogus")

    def test_config_setting_set_accepts_known_presets(self) -> None:
        assert validate_config_write("github_transport_preset", "webhook") == "webhook"
        assert validate_config_write("github_transport_preset", "polling") == "polling"
