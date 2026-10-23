"""Help text derived from each setting declaration."""

from functools import lru_cache

from teatree.config.declaration import declared_fields
from teatree.config.registries import COLD_HOOK_SETTINGS, COLD_SETTINGS, REGISTRY_SETTINGS
from teatree.config.settings import UserSettings


@lru_cache(maxsize=1)
def all_setting_help() -> dict[str, str]:
    cold = {**REGISTRY_SETTINGS, **COLD_SETTINGS, **COLD_HOOK_SETTINGS}
    return {
        **{key: row.help for key, row in declared_fields(UserSettings).items()},
        **{key: row.help for key, row in cold.items()},
    }


def setting_help(key: str) -> str:
    return all_setting_help().get(key, "")
