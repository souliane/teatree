"""DM-target resolution for the bot→user egress (split out of ``teatree.core.notify``).

The "who does the bot DM, and on which channel" concern: the canonical
``slack_user_id`` / ``slack_user_channel`` resolvers shared by every DM call
site (the :func:`teatree.core.notify.notify_user` egress and the
live-post-approval CLI verifier). Both walk the identical
overlay→global→sole-overlay→empty order so a change to the resolution order can
never drift between two private copies (the config-trap the #126 redesign
closes). Re-exported from ``teatree.core.notify`` so existing
``from teatree.core.notify import resolve_user_id`` call sites are unchanged.
"""

import os

from teatree.config import load_config


def resolve_user_id() -> str:
    """Resolve the Slack user id to DM (overlay override → global → sole overlay → empty).

    The per-overlay id comes from the DB overlays registry (still injected into
    ``load_config().raw["overlays"]``); the GLOBAL fallback reads the DB-home
    ``slack_user_id`` setting so every routing path agrees on the same order.

    The final ``sole overlay`` tier is the env-independent fallback that mirrors
    :func:`teatree.core.backend_factory.messaging_from_overlay` — the headless
    worker that DMs the owner does NOT export ``T3_OVERLAY_NAME``, so the
    overlay-scoped tier is skipped, and a fresh box carries no GLOBAL setting.
    When exactly one overlay is registered there is no ambiguity, so its own
    ``slack_user_id`` resolves without depending on the env var being set.
    """
    from teatree.config import cold_reader  # noqa: PLC0415 — deferred: call-time import, kept lazy

    cfg = load_config().raw
    overlay_name = os.environ.get("T3_OVERLAY_NAME", "")
    overlays = cfg.get("overlays") or {}
    if overlay_name and isinstance(overlays.get(overlay_name), dict):
        user_id = overlays[overlay_name].get("slack_user_id", "")
        if user_id:
            return str(user_id)
    global_id = cold_reader.str_setting("slack_user_id", default="")
    if global_id:
        return global_id
    return _sole_overlay_field(overlays, "slack_user_id")


def resolve_user_channel() -> str:
    """Resolve the Slack DM channel id the user reads (overlay → global → sole overlay → empty).

    The canonical resolver for the ``slack_user_channel`` config key,
    walking the SAME overlay→global→sole-overlay→empty order :func:`resolve_user_id`
    uses for ``slack_user_id``. Both DM-channel call sites (the bot→user
    DM path and the live-post-approval CLI verifier) consult this single
    helper, so a change to the resolution order can never drift between
    two private copies (the config-trap the #126 redesign closes).

    An empty return means no channel is configured; the caller treats it
    as "open a DM to the resolved user_id" rather than pinning to a
    specific ``D...`` channel.
    """
    from teatree.config import cold_reader  # noqa: PLC0415 — deferred: call-time import, kept lazy

    cfg = load_config().raw
    overlay_name = os.environ.get("T3_OVERLAY_NAME", "")
    overlays = cfg.get("overlays") or {}
    if overlay_name and isinstance(overlays.get(overlay_name), dict):
        channel = overlays[overlay_name].get("slack_user_channel", "")
        if channel:
            return str(channel)
    global_channel = cold_reader.str_setting("slack_user_channel", default="")
    if global_channel:
        return global_channel
    return _sole_overlay_field(overlays, "slack_user_channel")


def _sole_overlay_field(overlays: dict, key: str) -> str:
    """Return the sole registered overlay's ``key`` value, or ``""``.

    Env-independent fallback for the DM-target resolvers: when the active
    overlay is ambiguous (``T3_OVERLAY_NAME`` unset in the headless worker)
    and no GLOBAL setting is configured, a single registered overlay is
    unambiguous — its own value is the right target. Returns ``""`` when
    zero or more than one overlay is registered (ambiguous), so a
    multi-overlay box never silently picks the wrong owner.
    """
    if len(overlays) != 1:
        return ""
    entry = next(iter(overlays.values()))
    if not isinstance(entry, dict):
        return ""
    return str(entry.get(key, "") or "")


__all__ = ["resolve_user_channel", "resolve_user_id"]
