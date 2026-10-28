"""The stored DM rows each overlay's configured owner sent, resolved through that overlay's backend."""

from teatree.core.backend_factory import messaging_from_overlay
from teatree.core.models.pending_chat_injection import PendingChatInjectionQuerySet
from teatree.loop.owner_identity import owner_rows


def owner_dm_rows(rows: PendingChatInjectionQuerySet) -> PendingChatInjectionQuerySet:
    return owner_rows(rows, lambda overlay: messaging_from_overlay(overlay or None))
