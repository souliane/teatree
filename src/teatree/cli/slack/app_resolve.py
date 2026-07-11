"""Shared Slack app-id resolution + overlay-registry access for the setup commands.

``t3 setup slack-bot``, ``slack-user-token``, and ``slack-provision`` all need
the same answer to one question: *which Slack app id is this overlay's bot?*
Before this module each command grew its own copy of the resolution chain, and
``slack-bot --update`` skipped the chain entirely and prompted (#1686).

The resolution order is, for a given overlay:

1. the overlay's ``slack_app_id`` field in the DB ``overlays`` registry row
    (authoritative).
2. Derive from the overlay's bot token via ``auth.test`` + ``bots.info``.
3. Return ``""`` so the caller prompts or prints the manual fallback.

A newly-derived id is persisted back to the overlay's registry entry so step 2
runs at most once per overlay. Registry reads/writes go through
``ConfigSetting.objects`` — a read-modify-write against the single ``overlays``
row (the same DB-home shape :mod:`teatree.cli.slack.dm_provisioning` uses).
"""

import httpx

from teatree.utils.secrets import read_pass

_OVERLAYS_REGISTRY_KEY = "overlays"


def read_overlay_registry() -> dict[str, dict]:
    """Return the DB ``overlays`` registry (``{name: {fields}}``), or ``{}`` when unset."""
    from teatree.core.models import ConfigSetting  # noqa: PLC0415

    stored = ConfigSetting.objects.get_effective(_OVERLAYS_REGISTRY_KEY)
    return stored if isinstance(stored, dict) else {}


def write_overlay_fields(overlay: str, fields: dict[str, str]) -> None:
    """Merge *fields* into *overlay*'s entry of the DB ``overlays`` registry row.

    Creates the overlay's entry when absent so ``t3 setup slack-bot`` can record
    a bot on an overlay that had no registry fields yet.
    """
    from teatree.core.models import ConfigSetting  # noqa: PLC0415

    registry = read_overlay_registry()
    block = registry.get(overlay)
    if not isinstance(block, dict):
        block = {}
        registry[overlay] = block
    block.update(fields)
    ConfigSetting.objects.set_value(_OVERLAYS_REGISTRY_KEY, registry)


def read_overlay_field(overlay: str, field: str) -> str:
    """Return *overlay*'s *field* from the DB ``overlays`` registry, or ``""`` when unset."""
    block = read_overlay_registry().get(overlay)
    if not isinstance(block, dict):
        return ""
    return str(block.get(field, ""))


def persist_overlay_field(overlay: str, field: str, value: str) -> None:
    """Set *overlay*'s *field* in the DB ``overlays`` registry, updating an existing entry only.

    A no-op on an empty *value* or an overlay with no registry entry — the
    derive-and-cache path only augments an overlay the user already configured.
    """
    if not value or not isinstance(read_overlay_registry().get(overlay), dict):
        return
    write_overlay_fields(overlay, {field: value})


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


def resolve_overlay_app_id(overlay: str, *, token_ref: str = "") -> str:
    """Resolve *overlay*'s Slack app id from the registry, then derive it from its bot token.

    *token_ref* defaults to the overlay's recorded ``slack_token_ref``; the
    bot token is read from ``pass <token_ref>-bot``. A derived id is persisted
    back to the overlay's registry entry so the derivation runs at most once.
    Returns ``""`` when neither source resolves — the caller prompts or prints
    the manual fallback.
    """
    recorded = read_overlay_field(overlay, "slack_app_id")
    if recorded:
        return recorded
    ref = token_ref or read_overlay_field(overlay, "slack_token_ref")
    if not ref:
        return ""
    derived = derive_app_id_from_token(read_pass(f"{ref}-bot"))
    if derived:
        persist_overlay_field(overlay, "slack_app_id", derived)
    return derived


__all__ = [
    "derive_app_id_from_token",
    "persist_overlay_field",
    "read_overlay_field",
    "read_overlay_registry",
    "resolve_overlay_app_id",
    "write_overlay_fields",
]
