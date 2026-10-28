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
from collections.abc import Callable
from dataclasses import dataclass
from html import escape
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from teatree.config import load_config
from teatree.types import RawAPIDict

if TYPE_CHECKING:
    from teatree.core.backend_protocols import MessagingBackend
    from teatree.core.models import DeferredQuestion, Ticket


@runtime_checkable
class OperatorDmConfigurable(Protocol):
    def allow_operator_dms(self, resolver: Callable[[], frozenset[str]]) -> None: ...


@dataclass(frozen=True, slots=True)
class Addressee:
    user_id: str
    is_owner: bool = True
    fallback_requester: str = ""


def addressee_for(ticket: "Ticket", *, owner_id: str | None = None) -> Addressee:
    from teatree.core.models.operator_identity import (  # noqa: PLC0415 — CLI imports this module before Django setup
        OperatorIdentity,
        canonical_operator_key,
    )

    owner = resolve_user_id() if owner_id is None else owner_id
    if not ticket.requested_by:
        return Addressee(owner)
    try:
        key = canonical_operator_key(ticket.requested_by)
    except ValueError:
        return Addressee(owner)
    platform, handle = key.split(":", 1)
    identity = OperatorIdentity.objects.filter(
        platform=platform, handle=handle, user__is_active=True, user__is_superuser=True
    ).first()
    if identity is None:
        return Addressee(owner)
    slack = OperatorIdentity.objects.filter(user=identity.user, platform="slack").first()
    if slack is None:
        return Addressee(owner, fallback_requester=key)
    return Addressee(slack.handle, is_owner=slack.handle == owner)


def resolve_ticket_addressee(ticket_id: int | None, *, owner_id: str) -> Addressee:
    if ticket_id is None:
        return Addressee(owner_id)
    from teatree.core.models import Ticket  # noqa: PLC0415 — deferred model lookup avoids a core import cycle

    ticket = Ticket.objects.filter(pk=ticket_id).first()
    return addressee_for(ticket, owner_id=owner_id) if ticket is not None else Addressee(owner_id)


def question_addressee(question: "DeferredQuestion", *, owner_id: str) -> Addressee:
    from teatree.core.owner_dm import question_ticket  # noqa: PLC0415 — models unavailable during CLI bootstrap

    return resolve_ticket_addressee(question_ticket.single_question_ticket_id(question), owner_id=owner_id)


def label_fallback(text: str, blocks: list[RawAPIDict] | None, requester: str) -> tuple[str, list[RawAPIDict] | None]:
    if not requester:
        return text, blocks
    label = f"Requester: {escape(requester, quote=False)}"
    section: RawAPIDict = {"type": "section", "text": {"type": "mrkdwn", "text": label}}
    labeled_blocks: list[RawAPIDict] | None = [section, *blocks] if blocks is not None else None
    return f"{label}\n{text}", labeled_blocks


def registered_operator_slack_ids() -> frozenset[str]:
    from teatree.core.models import operator_identity  # noqa: PLC0415 — models unavailable during CLI bootstrap

    return frozenset(
        operator_identity.OperatorIdentity.objects.filter(
            platform="slack", user__is_active=True, user__is_superuser=True
        ).values_list("handle", flat=True)
    )


def configure_operator_dm_policy(backend: "MessagingBackend | None") -> "MessagingBackend | None":
    if isinstance(backend, OperatorDmConfigurable):
        backend.allow_operator_dms(registered_operator_slack_ids)
    return backend


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
    global_id = cold_reader.str_setting("slack_user_id")
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
    global_channel = cold_reader.str_setting("slack_user_channel")
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


__all__ = [
    "Addressee",
    "addressee_for",
    "configure_operator_dm_policy",
    "label_fallback",
    "question_addressee",
    "resolve_ticket_addressee",
    "resolve_user_channel",
    "resolve_user_id",
]
