"""Provisioning declaration group remains a flat dataclass base."""

from teatree.config.settings_provisioning import _ProvisioningSettings


def test_provisioning_group_defaults() -> None:
    settings = _ProvisioningSettings()
    assert settings.provision_step_timeout_seconds == 1800
    assert settings.clean_ignore == []
