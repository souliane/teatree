"""Shared Slack app-id resolution for the setup commands.

``t3 setup slack-bot``, ``slack-user-token``, and ``slack-provision`` all need
the same answer to one question: *which Slack app id is this overlay's bot?*
Before this module each command grew its own copy of the resolution chain, and
``slack-bot --update`` skipped the chain entirely and prompted (#1686).

The resolution order is, for a given overlay:

1. ``[overlays.<overlay>].slack_app_id`` in ``~/.teatree.toml`` (authoritative).
2. Derive from the overlay's bot token via ``auth.test`` + ``bots.info``.
3. Return ``""`` so the caller prompts or prints the manual fallback.

A newly-derived id is persisted back to the overlay block so step 2 runs at
most once per overlay.
"""

from pathlib import Path

import httpx

from teatree.utils.secrets import read_pass


def read_overlay_field(config_path: Path, overlay: str, field: str) -> str:
    """Return ``[overlays.<overlay>].<field>`` or ``""`` when unset."""
    if not config_path.is_file():
        return ""
    import tomlkit  # noqa: PLC0415

    document = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    block = document.get("overlays", {}).get(overlay) or {}
    return str(block.get(field, ""))


def persist_overlay_field(config_path: Path, overlay: str, field: str, value: str) -> None:
    """Write ``[overlays.<overlay>].<field> = value`` preserving the rest of the file."""
    if not value or not config_path.is_file():
        return
    import tomlkit  # noqa: PLC0415
    from tomlkit import items as tomlkit_items  # noqa: PLC0415

    document = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    overlays = document.get("overlays")
    if not isinstance(overlays, tomlkit_items.Table):
        return
    block = overlays.get(overlay)
    if not isinstance(block, tomlkit_items.Table):
        return
    block[field] = value
    config_path.write_text(tomlkit.dumps(document), encoding="utf-8")


def derive_app_id_from_token(token: str) -> str:
    """Derive the Slack app_id from any bot or user token via ``auth.test`` + ``bots.info``.

    Returns the empty string when derivation fails for any reason — callers
    fall back to the manual "open https://api.slack.com/apps" message.
    """
    if not token:
        return ""
    try:
        auth = httpx.post(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        auth.raise_for_status()
        auth_body = auth.json()
        if not auth_body.get("ok"):
            return ""
        bot_id = auth_body.get("bot_id")
        if not bot_id:
            return ""
        info = httpx.post(
            "https://slack.com/api/bots.info",
            headers={"Authorization": f"Bearer {token}"},
            data={"bot": bot_id},
            timeout=30,
        )
        info.raise_for_status()
        info_body = info.json()
        if not info_body.get("ok"):
            return ""
        app_id = (info_body.get("bot") or {}).get("app_id")
        return str(app_id) if app_id else ""
    except httpx.HTTPError:
        return ""


def resolve_overlay_app_id(config_path: Path, overlay: str, *, token_ref: str = "") -> str:
    """Resolve *overlay*'s Slack app id from config, then derive it from its bot token.

    *token_ref* defaults to the overlay's recorded ``slack_token_ref``; the
    bot token is read from ``pass <token_ref>-bot``. A derived id is persisted
    back to the overlay block so the derivation runs at most once. Returns
    ``""`` when neither source resolves — the caller prompts or prints the
    manual fallback.
    """
    recorded = read_overlay_field(config_path, overlay, "slack_app_id")
    if recorded:
        return recorded
    ref = token_ref or read_overlay_field(config_path, overlay, "slack_token_ref")
    if not ref:
        return ""
    derived = derive_app_id_from_token(read_pass(f"{ref}-bot"))
    if derived:
        persist_overlay_field(config_path, overlay, "slack_app_id", derived)
    return derived


__all__ = [
    "derive_app_id_from_token",
    "persist_overlay_field",
    "read_overlay_field",
    "resolve_overlay_app_id",
]
