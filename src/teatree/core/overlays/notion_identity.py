"""Which registered overlay's Notion identity a ``t3 notion`` call runs as, and how to route one that has none.

One answer for the CLI, ``t3 doctor`` and every other ``build_notion_client`` caller: a named overlay
(``--overlay``, else ``T3_OVERLAY_NAME``) is folded onto its registered name; an unnamed call runs as
the overlay the venue's own Notion routing singles out, and refuses rather than pick between two
identities.
"""

import os
from typing import TYPE_CHECKING

from django.core.exceptions import ImproperlyConfigured

from teatree.backends.types import Service
from teatree.config import OverlayEntry
from teatree.core import overlay_loader
from teatree.core.overlay_name_resolution import resolve_overlay_name

if TYPE_CHECKING:
    from teatree.core.overlay import OverlayConfig

NOTION_CREDENTIAL = "notion_token"
NOTION_CREDENTIAL_ENV_VAR = "NOTION_TOKEN"


def notion_routed_overlays() -> "list[tuple[str, OverlayConfig]]":
    """``(overlay, config)`` for every registered overlay that needs Notion or routes a token entry."""
    return [
        (name, overlay.config)
        for name, overlay in sorted(overlay_loader.get_all_overlays().items())
        if Service.NOTION in overlay.config.required_third_party_services
        or overlay.config.secret_pass_key(NOTION_CREDENTIAL)
    ]


def notion_overlay(requested: str | None = None) -> str | None:
    """The overlay a Notion call runs as, or ``None`` when no overlay routes Notion at all."""
    if name := (requested or os.environ.get("T3_OVERLAY_NAME") or "").strip():
        return resolve_overlay_name(name) or name
    routed = notion_routed_overlays()
    if len(routed) <= 1:
        return routed[0][0] if routed else None
    exported = bool(os.environ.get(NOTION_CREDENTIAL_ENV_VAR))
    entries = {config.secret_pass_key(NOTION_CREDENTIAL) for _name, config in routed} - {""}
    declaring = [name for name, config in routed if Service.NOTION in config.required_third_party_services]
    if (exported or len(entries) <= 1) and len(declaring) == 1:
        return declaring[0]
    listed = ", ".join(f"{name} (pass {config.secret_pass_key(NOTION_CREDENTIAL) or '-'})" for name, config in routed)
    undecided = (
        f"${NOTION_CREDENTIAL_ENV_VAR} decides the identity, but not whose write roots apply"
        if exported
        else "`t3 notion` never picks between Notion identities"
    )
    msg = (
        f"no --overlay was given and several overlays route Notion: {listed}. {undecided} — name the one to "
        f"run as, e.g. `--overlay {routed[0][0]}` or `T3_OVERLAY_NAME={routed[0][0]}`."
    )
    raise ImproperlyConfigured(msg)


def route_notion_token_command(overlay: str) -> str:
    cli = OverlayEntry.canonical_overlay_name(overlay)
    return f"t3 {cli} config_setting set notion_token_pass_key '\"<entry>\"' --overlay {overlay}"


def missing_notion_token_reason(overlay: str | None, pass_key: str) -> str:
    """Why no token resolved for *overlay*, ending in the one command that fixes that case."""
    registered = sorted(overlay_loader.get_all_overlays())
    if overlay is None:
        return (
            f"no registered overlay routes a Notion token on this venue (registered: {', '.join(registered)}). "
            f"Route one: `{route_notion_token_command('<overlay>')}`"
        )
    if overlay not in registered:
        return (
            f"no overlay named {overlay!r} is registered (registered: {', '.join(registered)}). "
            "Pass one of them to --overlay"
        )
    if not pass_key:
        return (
            f"overlay {overlay!r} routes no notion_token_pass_key on this venue. "
            f"Route it: `{route_notion_token_command(overlay)}`, then store the token: "
            f"`t3 notion setup --overlay {overlay}`"
        )
    return (
        f"`pass {pass_key}`, which overlay {overlay!r} routes, is empty on this venue. "
        f"Store the token: `t3 notion setup --overlay {overlay}`"
    )
