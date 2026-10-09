"""The MCP self-DM gate and the destination ids it refuses.

:func:`handle_block_self_dm_via_mcp` refuses a Slack MCP write to the operator's own
bot<->user DM. The ids come DB-only: the DB-home ``overlays`` registry and the global
``slack_user_id`` setting, read via the Django-free ``teatree.config.cold_reader``. The
router keeps only the registration — the same bare-sibling pattern ``managed_repo`` /
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
from typing import Any

from hooks.scripts.managed_repo import teatree_src_on_path
from hooks.scripts.mcp_slack_write_guard import is_slack_mcp_write

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
            value = cfg.get(key)
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


_UNREADABLE_REASON = (
    "SELF-DM REFUSED (fail-closed): could not read the bot↔user DM destination ids "
    "from the config store (the DB is missing, locked, or unreadable), so this gate "
    "cannot confirm the Slack MCP write is not a self-DM under the USER's token. "
    "Declare the per-overlay slack_dm_channel_id / slack_user_id keys via "
    "`t3 <overlay> config_setting set`, or disable this gate with "
    "`t3 <overlay> config_setting set self_dm_gate_enabled false`. "
    "To DM the user now, use the bot-token path: "
    "`t3 teatree notify send -` (reads the body from stdin)."
)


def _gate_enabled() -> bool:
    try:
        from hooks.scripts.hook_router import _teatree_bool_setting  # noqa: PLC0415 deferred back-import

        return _teatree_bool_setting("self_dm_gate_enabled", default=True)
    except Exception:  # noqa: BLE001 — a config-read error must never wedge the tool call.
        return True


def handle_block_self_dm_via_mcp(data: dict) -> bool:
    """Refuse a Slack MCP write to the operator's own bot↔user DM under the user's token.

    A Slack MCP server other than teatree's writes under the USER's token, so a post or
    reaction in the operator's self-IM renders as user-authored and the loop's scanners
    react to the agent's own message as if the owner wrote it. teatree's egress
    chokepoints never see an MCP call, so this deny is the only place to stop it.

    What counts as a write is the Slack write guard's default-deny classifier
    (:func:`~hooks.scripts.mcp_slack_write_guard.is_slack_mcp_write`), so the two agree.
    Unlike that guard, nothing on the call lifts this deny — no ``[slack-mcp-ok: …]``
    token (which an agent can add itself), no ``danger_gate_fail_open``, and not the
    guard's own kill-switch, the setting under which a self-DM write would otherwise
    pass. Only the operator's ``self_dm_gate_enabled = false`` does, for a misfiring
    registry. Mirroring ``SlackBotBackend._is_self_dm``, a self-DM id is a configured
    ``[overlays.*].slack_dm_channel_id`` (``D…``) or ``slack_user_id`` / global
    ``slack_user_id`` (``U…``, which Slack opens as the self-IM).

    FAIL-CLOSED (user decision): an unreachable config store denies, because the hook
    cannot identify the author without it; a readable store declaring no ids allows.
    """
    if not is_slack_mcp_write(data.get("tool_name", "")):
        return False
    tool_input = data.get("tool_input", {}) or {}
    if not isinstance(tool_input, dict) or not _gate_enabled():
        return False
    from hooks.scripts.hook_router import emit_pretooluse_deny  # noqa: PLC0415 deferred back-import

    destinations = read_self_dm_destinations()
    if not destinations.resolved:
        return emit_pretooluse_deny(_UNREADABLE_REASON, gate_id="self_dm")
    destination = self_dm_destination(tool_input, destinations.ids)
    if not destination:
        return False
    return emit_pretooluse_deny(
        f"SELF-DM REFUSED: this Slack MCP write targets the operator's own bot↔user DM "
        f"({destination}) under the USER's token, so it renders as user-authored and the "
        f"loop's scanners will react to the agent's own message. Use the bot-token path "
        f"instead: `t3 teatree notify send -` (reads the body from stdin). Posts to "
        f"colleague channels are unaffected by this gate.",
        gate_id="self_dm",
    )
