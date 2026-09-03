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
UNREACHABLE one (fail-closed deny) via a config-store reachability probe that, like
the id reads beside it, counts the published host projection as a readable store.
"""

import dataclasses
import sys
from types import ModuleType
from typing import Any, cast

from hooks.scripts.managed_repo import teatree_src_on_path

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
    config, so a can't-read config must not let a self-DM through). An ABSENT
    canonical DB is not unreadable: on a host the control DB lives in a container
    volume, and the ids come from the published host projection instead.
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
            value = cast("dict[str, object]", cfg).get(key)
            if isinstance(value, str) and value:
                ids.add(value)
    return ids


def _config_store_reachable(cold_reader: ModuleType) -> bool:
    """Whether the store the id reads resolve could be READ at all.

    Two ways to be reachable, because the id readers themselves have two: the canonical
    DB answers the probe, OR — the ordinary host case, the control DB living in a
    container volume — that DB file is simply ABSENT and ``read_setting`` serves the ids
    from the published host projection. A canonical DB that is PRESENT but unreadable is
    neither: its reads yield nothing and no projection fallback applies, so an empty id
    set there is a read failure rather than a genuinely-empty config.
    """
    if cold_reader.row_exists(_CONFIG_STORE_PROBE, on_error=False):
        return True
    return not cold_reader.canonical_config_db().exists() and cold_reader.canonical_projection() is not None


def read_self_dm_destinations() -> SelfDmDestinations:
    """Assemble the self-DM ids from the DB-home ``overlays`` registry + global ``slack_user_id``.

    ``resolved`` is ``False`` (fail-closed deny) only when the config store is
    UNREACHABLE — a locked/corrupt DB, an absent config table, an absent canonical DB
    with no projection published, or a ``teatree`` that won't import; a reachable store
    with no ids is ``resolved`` + empty (allow silently). The reachability probe
    (``SELECT count(*)`` against ``teatree_config_setting``) always yields a row when
    the table exists — even empty — so it separates "readable, nothing declared" from
    "unreadable" cleanly, where the fail-open ``read_setting`` reads alone cannot. The
    overlay ids come from the ``overlays`` row (``slack_dm_channel_id`` /
    ``slack_user_id`` per overlay); the global ``slack_user_id`` mirrors
    ``notify.resolve_user_id``.
    """
    try:
        with teatree_src_on_path():
            from teatree.config import cold_reader  # noqa: PLC0415 — deferred: cold-hook import after sys.path setup

            if not _config_store_reachable(cold_reader):
                return SelfDmDestinations(frozenset(), resolved=False)
            overlays = cold_reader.mapping_setting("overlays")
            global_user_id = cold_reader.str_setting("slack_user_id", default="")
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return SelfDmDestinations(frozenset(), resolved=False)
    ids = overlay_slack_ids(overlays)
    if global_user_id:
        ids.add(global_user_id)
    return SelfDmDestinations(frozenset(ids), resolved=True)


#: The ``tool_input`` keys a Slack MCP write names its destination with. Both
#: spellings appear across the tool surface, so both are consulted.
_CHANNEL_FIELDS: tuple[str, ...] = ("channel", "channel_id")


def slack_tool_suffix(tool_name: str) -> str:
    """The bare Slack tool name behind an MCP-qualified ``mcp__<server>__<tool>``."""
    return tool_name.rsplit("__", 1)[-1]


def self_dm_destination(tool_input: dict, dm_ids: frozenset[str]) -> str:
    """The self-DM destination *tool_input* targets, or ``""`` when it targets none.

    A write is a self-DM only when its named destination is one of the resolved ids;
    an unrecognised or absent destination is not a self-DM, so it is left alone.
    """
    for field in _CHANNEL_FIELDS:
        value = tool_input.get(field)
        if isinstance(value, str) and value in dm_ids:
            return value
    return ""
