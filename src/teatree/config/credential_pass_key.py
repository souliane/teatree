"""Which ``pass`` entry a credential reads — a per-venue setting, never a core default.

One credential lives at a different entry on each venue's store, so the entry NAME
resolves like any other setting, first match wins:

1. a ``<credential>_pass_key`` ``ConfigSetting`` row in the overlay's scope;
2. the same row in the global scope;
3. the overlay's declared ``<CREDENTIAL>_PASS_KEY`` constant;
4. nothing — the credential is unconfigured and reads no entry.

A credential that also accepts an env var (``NOTION_TOKEN``) consults it BEFORE any of
this; the env value is the secret itself, so no entry is read at all. A store that
could not be read resolves to nothing rather than to the declared default, because a
venue that repointed the key must never silently read the entry it moved away from.
A config bound to no overlay scope (built outside discovery) has only its declared default.
"""

import dataclasses
from enum import StrEnum

from teatree.config.override_reader import load_global_rows, load_overlay_rows
from teatree.config.secret_settings import CREDENTIAL_ENTRY_SUFFIX, is_pass_key_setting

#: Read by core whether or not any overlay declares a default entry for them.
CORE_CREDENTIALS: frozenset[str] = frozenset(
    {"figma_token", "github_token", "gitlab_token", "gitlab_username", "notion_token", "sentry_token", "slack_token"}
)


class PassKeySource(StrEnum):
    OVERLAY_DB = "db, overlay scope"
    GLOBAL_DB = "db, global scope"
    DECLARED_DEFAULT = "overlay declared default"
    UNSET = "unset"
    UNREADABLE = "config store unreadable"


@dataclasses.dataclass(frozen=True, slots=True)
class PassKeyResolution:
    setting: str
    value: str
    source: PassKeySource


def pass_key_setting(credential: str) -> str:
    return f"{credential}{CREDENTIAL_ENTRY_SUFFIX}"


def credential_of_setting(setting: str) -> str | None:
    return setting.removesuffix(CREDENTIAL_ENTRY_SUFFIX) if is_pass_key_setting(setting) else None


def validate_pass_key_entry(value: object) -> str:
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        msg = f"a pass entry name is a non-empty string with no whitespace, not {value!r}"
        raise ValueError(msg)
    return value


def resolve_pass_key(credential: str, *, overlay_name: str, declared_default: str) -> PassKeyResolution:
    setting = pass_key_setting(credential)
    if not overlay_name:
        return _declared(setting, declared_default)
    overlay_rows, overlay_degraded = load_overlay_rows(overlay_name)
    if overlay_degraded:
        return PassKeyResolution(setting, "", PassKeySource.UNREADABLE)
    if stored := overlay_rows.get(setting):
        return PassKeyResolution(setting, str(stored), PassKeySource.OVERLAY_DB)
    global_rows, global_degraded = load_global_rows()
    if global_degraded:
        return PassKeyResolution(setting, "", PassKeySource.UNREADABLE)
    if stored := global_rows.get(setting):
        return PassKeyResolution(setting, str(stored), PassKeySource.GLOBAL_DB)
    return _declared(setting, declared_default)


def _declared(setting: str, declared_default: str) -> PassKeyResolution:
    if declared_default:
        return PassKeyResolution(setting, declared_default, PassKeySource.DECLARED_DEFAULT)
    return PassKeyResolution(setting, "", PassKeySource.UNSET)
