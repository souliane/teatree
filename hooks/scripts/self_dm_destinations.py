"""Self-DM destination-id resolution for the MCP self-DM gate.

The PURE assembly behind ``hook_router.handle_block_self_dm_via_mcp``: given the
two raw config sources (the DB-home ``overlays`` registry and the parsed
``~/.teatree.toml``), compute the operator's own bot<->user DM destination ids.
Extracted from ``hook_router`` (the shrink-only god-module) so the router keeps
only the IO (the toml read, which a test patches via ``router.Path``) and this
sibling keeps the testable logic — the same bare-sibling pattern
``managed_repo`` / ``deny_circuit_breaker`` use. Cold-import safe: stdlib only.

DB-first (eliminate-~/.teatree.toml): the overlay registry resolves from the
DB-home ``overlays`` row when present, so a DELETED toml still self-identifies the
operator; the global ``[teatree] slack_user_id`` stays toml-home (mirrors
``notify.resolve_user_id``).
"""

import dataclasses
import sys
from typing import Any

# Alias both identities so a bare ``from self_dm_destinations import ...`` (the
# live hook, whose dir is on sys.path) and ``hooks.scripts.self_dm_destinations``
# (a test import) resolve the SAME module object — the pattern every sibling uses.
sys.modules.setdefault("self_dm_destinations", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.self_dm_destinations", sys.modules[__name__])


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


def resolve_self_dm_destinations(
    db_overlays: dict[str, Any] | None, toml_config: dict[str, Any] | None
) -> SelfDmDestinations:
    """Assemble the self-DM ids from the DB overlay registry + the parsed toml.

    DB-first: the overlay ids come from *db_overlays* when present (a DELETED toml
    still self-identifies the operator), else the toml ``[overlays.*]`` tables. The
    global ``[teatree] slack_user_id`` stays toml-home. ``resolved`` is ``False``
    only when NEITHER source was readable (fail-closed deny); a readable source
    with no ids is ``resolved`` + empty (allow silently).
    """
    if not db_overlays and toml_config is None:
        return SelfDmDestinations(frozenset(), resolved=False)
    ids = overlay_slack_ids(db_overlays or (toml_config.get("overlays") if toml_config else None))
    teatree = toml_config.get("teatree") if isinstance(toml_config, dict) else None
    if isinstance(teatree, dict) and isinstance(teatree.get("slack_user_id"), str) and teatree["slack_user_id"]:
        ids.add(teatree["slack_user_id"])
    return SelfDmDestinations(frozenset(ids), resolved=True)
