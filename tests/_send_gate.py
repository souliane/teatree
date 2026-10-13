"""Outbound-gate test helpers, re-exporting the placeholder term registry for their importers."""

from tests.placeholder_terms import TEST_TERM_REGISTRY, TEST_TERM_REGISTRY_JSON

__all__ = ["TEST_TERM_REGISTRY", "TEST_TERM_REGISTRY_JSON", "allow_forge_repos", "allow_slack_channels"]


def allow_forge_repos(*repos: str) -> None:
    """Allow forge writes by repo slug, including creates with no URL to identify the forge."""
    from teatree.core.models import ConfigSetting  # noqa: PLC0415 — test helper keeps collection Django-free

    ConfigSetting.objects.set_value("send_proxy_allowlist", list(repos))


def allow_slack_channels(*channels: str) -> None:
    """Install the Slack destinations used by a transport or posture test."""
    from teatree.core.models import ConfigSetting  # noqa: PLC0415 — test helper keeps collection Django-free

    ConfigSetting.objects.set_value("send_proxy_allowlist", list(channels))
