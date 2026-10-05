"""Neutral, classed term registry for tests that exercise real outbound gates."""

import json

TEST_TERM_REGISTRY = {
    "leak": ["quartzbridge-bank"],
    "prose_collider": ["meridian-ledger"],
    "overlay": ["quartzbridge-overlay"],
}
TEST_TERM_REGISTRY_JSON = json.dumps(TEST_TERM_REGISTRY)


def allow_forge_repos(*repos: str) -> None:
    """Allow forge writes by repo slug, including creates with no URL to identify the forge."""
    from teatree.core.models import ConfigSetting  # noqa: PLC0415 — test helper keeps collection Django-free

    ConfigSetting.objects.set_value("send_proxy_allowlist", list(repos))


def allow_slack_channels(*channels: str) -> None:
    """Install the Slack destinations used by a transport or posture test."""
    from teatree.core.models import ConfigSetting  # noqa: PLC0415 — test helper keeps collection Django-free

    ConfigSetting.objects.set_value("send_proxy_allowlist", list(channels))
