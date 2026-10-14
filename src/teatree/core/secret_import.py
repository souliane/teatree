"""Read-only inventory for copying password-store entries into sealed rows."""

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from teatree.config.credential_pass_key import CORE_CREDENTIALS
from teatree.config.secret_settings import is_credential_reference
from teatree.core.models.anthropic_active_pick import AnthropicActivePick
from teatree.core.models.anthropic_token_usage import AnthropicTokenUsage
from teatree.core.models.config_setting import ConfigSetting
from teatree.core.models.worktree import Worktree
from teatree.core.overlay_loader import get_all_overlays
from teatree.utils.secrets import PassStore, SecretStore, SecretStoreError

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
_PROBE = "teatree/slack-app-config-issued-at-writability-probe"
_MANAGED_PREFIXES = ("teatree/github-webhook/", "anthropic/")
_ENV_PASS_PATHS = (
    "T3_ADMIN_PASSWORD_PASS_PATH",
    "TEATREE_GH_TOKEN_PASS_PATH",
    "TEATREE_GITLAB_TOKEN_PASS_PATH",
    "NOTION_TOKEN_PASS_PATH",
)


@dataclass(frozen=True, slots=True)
class ImportEntry:
    name: str
    destination: str
    readability: str


def _values_at(key: str, value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for child_key, child_value in value.items():
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


class PasswordStoreDirectoryError(FileNotFoundError):
    def __init__(self, root: Path) -> None:
        super().__init__(f"password store directory is unreadable: {root}")


def _present_names(root: Path) -> frozenset[str]:
    if root.is_symlink() or not root.is_dir():
        raise PasswordStoreDirectoryError(root)
    names: set[str] = set()

    def raise_walk_error(error: OSError) -> None:
        raise error

    for directory, _, files in os.walk(root, followlinks=False, onerror=raise_walk_error):
        for filename in files:
            path = Path(directory) / filename
            if filename.endswith(".gpg") and path.is_file() and not path.is_symlink():
                names.add(path.relative_to(root).as_posix().removesuffix(".gpg"))
    return frozenset(names)


def _readability(store: SecretStore, name: str) -> str:
    try:
        return "readable" if store.read(name) is not None else "missing"
    except (SecretStoreError, FileNotFoundError):
        return "unreadable"


def build_import_plan(*, store: SecretStore | None = None, root: Path | None = None) -> tuple[ImportEntry, ...]:
    source = store if store is not None else PassStore()
    store_root = (
        root if root is not None else Path(os.environ.get("PASSWORD_STORE_DIR") or Path.home() / ".password-store")
    )
    referenced = referenced_entry_names()
    present = _present_names(store_root)
    entries: list[ImportEntry] = []
    for name in sorted(present | referenced):
        if name == _PROBE:
            destination = "drop"
        elif (
            name in referenced
            or name.startswith(_MANAGED_PREFIXES)
            or (".bak-" in name and name.partition(".bak-")[0] in referenced)
        ):
            destination = "row" if name in present else "missing"
        else:
            destination = "external"
        readability = (
            _readability(source, name)
            if name in present and destination == "row"
            else "not-checked"
            if name in present
            else "missing"
        )
        entries.append(ImportEntry(name, destination, readability))
    return tuple(entries)
