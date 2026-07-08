"""Self-DM destination-id resolution for the MCP self-DM gate.

The DB-only assembly behind ``hook_router.handle_block_self_dm_via_mcp``: read the
DB-home ``overlays`` registry and the global ``slack_user_id`` setting via the
Django-free ``teatree.config.cold_reader``, then compute the operator's own
bot<->user DM destination ids. Extracted from ``hook_router`` (the shrink-only
god-module) so the router keeps only the thin call site and this sibling owns the
testable logic — the same bare-sibling pattern ``managed_repo`` /
``deny_circuit_breaker`` use.

The overlay ids come from the DB-home ``overlays`` row; the global ``slack_user_id``
mirrors ``notify.resolve_user_id``'s global fallback (also DB-home). ``resolved``
distinguishes a READABLE config store with no ids (allow silently) from an
UNREACHABLE one (fail-closed deny) via a config-store reachability probe.
"""

import dataclasses
import sys
from typing import Any

from managed_repo import teatree_src_on_path

# Alias both identities so a bare ``from self_dm_destinations import ...`` (the
# live hook, whose dir is on sys.path) and ``hooks.scripts.self_dm_destinations``
# (a test import) resolve the SAME module object — the pattern every sibling uses.
sys.modules.setdefault("self_dm_destinations", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.self_dm_destinations", sys.modules[__name__])

_CONFIG_STORE_PROBE = "SELECT count(*) FROM teatree_config_setting"


@dataclasses.dataclass(frozen=True)
class SelfDmDestinations:
    """Resolved set of self-DM destination ids, with a read-success flag.

    The set mirrors the canonical ``SlackBotBackend._is_self_dm``: each
    overlay's ``slack_dm_channel_id`` (the ``D…`` self-IM id) AND each
    ``slack_user_id`` plus the global ``[teatree] slack_user_id`` (the
    ``U…`` id Slack accepts as a target that opens the self-IM).

    ``resolved`` distinguishes a genuinely-empty configuration (nothing
    declared → ALLOW silently) from an unreadable/unparsable one
    (→ DENY fail-closed: the hook cannot self-identify the author without the
    config, so a can't-read config must not let a self-DM through).
    """

    ids: frozenset[str]
    resolved: bool


def overlay_slack_ids(overlays: dict[str, Any] | None) -> set[str]:
    """Each overlay's ``slack_dm_channel_id`` + ``slack_user_id`` from an overlays registry dict."""
    ids: set[str] = set()
    if not isinstance(overlays, dict):
        return ids
    for cfg in overlays.values():
        if not isinstance(cfg, dict):
            continue
        for key in ("slack_dm_channel_id", "slack_user_id"):
            value = cfg.get(key)
            if isinstance(value, str) and value:
                ids.add(value)
    return ids


def read_self_dm_destinations() -> SelfDmDestinations:
    """Assemble the self-DM ids from the DB-home ``overlays`` registry + global ``slack_user_id``.

    DB-only. ``resolved`` is ``False`` (fail-closed deny) only when the config store
    is UNREACHABLE — a missing/locked/corrupt DB, an absent config table, or a
    ``teatree`` that won't import; a reachable store with no ids is ``resolved`` +
    empty (allow silently). The reachability probe (``SELECT count(*)`` against
    ``teatree_config_setting``) always yields a row when the table exists — even
    empty — so it separates "readable, nothing declared" from "unreadable" cleanly,
    where the fail-open ``read_setting`` reads alone cannot. The overlay ids come
    from the ``overlays`` row (``slack_dm_channel_id`` / ``slack_user_id`` per
    overlay); the global ``slack_user_id`` mirrors ``notify.resolve_user_id``.
    """
    try:
        with teatree_src_on_path():
            from teatree.config import cold_reader  # noqa: PLC0415

            if not cold_reader.row_exists(_CONFIG_STORE_PROBE, on_error=False):
                return SelfDmDestinations(frozenset(), resolved=False)
            overlays = cold_reader.read_setting("overlays")
            global_user_id = cold_reader.str_setting("slack_user_id", default="")
    except Exception:  # noqa: BLE001
        return SelfDmDestinations(frozenset(), resolved=False)
    ids = overlay_slack_ids(overlays if isinstance(overlays, dict) else None)
    if global_user_id:
        ids.add(global_user_id)
    return SelfDmDestinations(frozenset(ids), resolved=True)
