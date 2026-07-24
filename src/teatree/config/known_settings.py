"""The unified known-key set every ``config_setting`` surface consults.

One dict uniting the four config-key registries — the ``UserSettings`` DB
partition (``OVERLAY_OVERRIDABLE_SETTINGS``), the injected registries
(``REGISTRY_SETTINGS``), the cold-read keys (``COLD_SETTINGS``), and the
pre-Django cold-hook gate flags (``COLD_HOOK_SETTINGS``) — each mapped to its
write-time parser. The CLI ``config_setting`` command and the MCP
``config_setting_get`` read tool both resolve key-ness through THIS dict, so
the two surfaces can never disagree about which keys exist: a key the CLI can
``set`` is a key the MCP read reports ``known``.

A leaf below the four registry modules (imports them, imported by neither), so
it closes the union without an import cycle.
"""

from collections.abc import Callable
from typing import Any

from teatree.config.cold_hook_settings import COLD_HOOK_SETTINGS
from teatree.config.registries import COLD_SETTINGS, REGISTRY_SETTINGS
from teatree.config.setting_registries import OVERLAY_OVERRIDABLE_SETTINGS

ALL_KNOWN_CONFIG_SETTINGS: dict[str, Callable[[Any], Any]] = {
    **OVERLAY_OVERRIDABLE_SETTINGS,
    **REGISTRY_SETTINGS,
    **COLD_SETTINGS,
    **{key: setting.parse for key, setting in COLD_HOOK_SETTINGS.items()},
}
