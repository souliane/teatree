"""Configured Slack owner ids shared by stored-DM readers."""

from teatree.core.models import ConfigSetting


def configured_slack_owner_ids() -> dict[str, str]:
    registry = ConfigSetting.objects.get_effective("overlays")
    if not isinstance(registry, dict):
        return {}
    return {
        overlay: block["slack_user_id"].strip()
        for overlay, block in registry.items()
        if isinstance(block, dict) and isinstance(block.get("slack_user_id"), str) and block["slack_user_id"].strip()
    }
