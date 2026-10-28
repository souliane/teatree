"""The pass-entry names the installation's settings, overlays and rows reference."""

import os
from collections.abc import Iterator

from teatree.config.credential_pass_key import CORE_CREDENTIALS
from teatree.config.secret_settings import is_credential_reference
from teatree.core.models.anthropic_active_pick import AnthropicActivePick
from teatree.core.models.anthropic_token_usage import AnthropicTokenUsage
from teatree.core.models.config_setting import ConfigSetting
from teatree.core.models.worktree import Worktree
from teatree.core.overlay_loader import get_all_overlays

_FIXED_ENTRIES = frozenset(
    {
        "teatree/slack-app-config-token",
        "teatree/slack-app-config-refresh",
        "teatree/slack-app-config-issued-at",
        "teatree/codex/auth-json-b64",
        "teatree/codex/ro-executor-auth-json-b64",
        "teatree/admin-password",
        "slack/user-id",
        "slack/user-oauth-token",
        "slack/bot-token",
    }
)
_ENV_PASS_PATHS = (
    "T3_ADMIN_PASSWORD_PASS_PATH",
    "TEATREE_GH_TOKEN_PASS_PATH",
    "TEATREE_GITLAB_TOKEN_PASS_PATH",
    "NOTION_TOKEN_PASS_PATH",
)


def _values_at(key: str, value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            if key.endswith("pass_paths") and isinstance(child_value, str) and child_value:
                yield child_value
            yield from _values_at(str(child_key), child_value)
    elif isinstance(value, list):
        for item in value:
            yield from _values_at(key, item)
    elif isinstance(value, str) and value and is_credential_reference(key):
        if key == "slack_token_ref":
            yield f"{value}-bot"
            yield f"{value}-app"
        else:
            yield value


def referenced_entry_names() -> frozenset[str]:
    names: set[str] = set(_FIXED_ENTRIES)
    names.update(value for key in _ENV_PASS_PATHS if (value := os.environ.get(key)))
    for key, value in ConfigSetting.objects.values_list("key", "value"):
        names.update(_values_at(key, value))
    names.update(AnthropicTokenUsage.objects.exclude(pass_path="").values_list("pass_path", flat=True))
    names.update(AnthropicActivePick.objects.exclude(pass_path="").values_list("pass_path", flat=True))
    names.update(worktree.pass_key for worktree in Worktree.objects.all())
    for overlay in get_all_overlays().values():
        for credential in CORE_CREDENTIALS | overlay.config.declared_credentials():
            resolution = overlay.config.resolve_pass_key(credential)
            if resolution.value:
                names.add(resolution.value)
        names.update(_values_at("slack_token_ref", overlay.config.slack_token_ref))
        names.update(_values_at("user_token_ref", overlay.config.user_token_ref))
    return frozenset(names)
