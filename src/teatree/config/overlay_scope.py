"""Whether an overlay row can reach a reader for the named setting."""

from teatree.config.known_settings import SETTING_ENTRIES
from teatree.config.setting_registries import BOX_GLOBAL_SETTINGS, OVERLAY_OVERRIDABLE_SETTINGS


def unhonourable_overlay_scope_reason(key: str, scope: str) -> str | None:
    if scope and key in BOX_GLOBAL_SETTINGS:
        return (
            f"{key!r} is one clock per box, so it takes no overlay scope — its reader resolves no "
            f"overlay, which makes a row scoped to {scope!r} either unreachable or decided by "
            "whichever overlay the process happens to be. Set it globally instead."
        )
    if scope and key in SETTING_ENTRIES and key not in OVERLAY_OVERRIDABLE_SETTINGS:
        return (
            f"{key!r} resolves at the global scope only — its reader (a cold/registry read) takes no "
            f"overlay, so a row scoped to {scope!r} would never be read. Set it globally instead."
        )
    return None


def reject_unhonourable_overlay_scope(key: str, scope: str) -> None:
    if reason := unhonourable_overlay_scope_reason(key, scope):
        raise ValueError(reason)
