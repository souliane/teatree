"""The unified known-key set every ``config_setting`` surface consults.

One dict uniting the four config-key registries — the ``UserSettings`` DB
partition (``OVERLAY_OVERRIDABLE_SETTINGS``), the injected registries
(``REGISTRY_SETTINGS``), the cold-read keys (``COLD_SETTINGS``), and the
pre-Django cold-hook gate flags (``COLD_HOOK_SETTINGS``) — each mapped to its
write-time parser. The CLI ``config_setting`` command and the MCP
``config_setting_get`` read tool both resolve key-ness through THIS dict, so
the two surfaces can never disagree about which keys exist: a key the CLI can
``set`` is a key the MCP read reports ``known``.

Membership here answers "is this key a live SETTING". What a key OUTSIDE the union
is — retired, internal runtime state, or an orphan — is
``teatree.config.stored_row_health``, which builds on this dict.

A leaf below the two registry modules (imports them, imported by neither), so
it closes the union without an import cycle.
"""

from collections.abc import Callable
from typing import Any

from teatree.config.registries import COLD_HOOK_SETTINGS, COLD_SETTINGS, REGISTRY_SETTINGS, ColdHookSetting
from teatree.config.setting_registries import OVERLAY_OVERRIDABLE_SETTINGS

#: Every key with NO ``UserSettings`` field, paired with the entry that declares it —
#: parser AND shipped default. The resolver's default authority reads this side; the
#: write-validation surfaces below read the parser side.
SETTING_ENTRIES: dict[str, ColdHookSetting] = {**REGISTRY_SETTINGS, **COLD_SETTINGS, **COLD_HOOK_SETTINGS}

ALL_KNOWN_CONFIG_SETTINGS: dict[str, Callable[[Any], Any]] = {
    **OVERLAY_OVERRIDABLE_SETTINGS,
    **{key: entry.parse for key, entry in SETTING_ENTRIES.items()},
}
