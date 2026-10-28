"""Who the configured Slack owner is, and which senders and stored DM rows are theirs.

The one owner policy every inbound DM path shares: the recorder and the drain scanner
decide on the event's sender, and every consumer of stored rows reads only the rows the
owner sent. An owner id that cannot be read is nobody, so every check fails closed.
"""

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from teatree.types import RawAPIDict

if TYPE_CHECKING:
    from teatree.core.models.pending_chat_injection import PendingChatInjectionQuerySet

logger = logging.getLogger(__name__)


def configured_owner_id(backend: object) -> str:
    return str(getattr(backend, "user_id", "") or "")


def is_the_owner(user_id: object, owner_user_id: str) -> bool:
    return bool(owner_user_id) and user_id == owner_user_id


def sent_by_owner(event: RawAPIDict, owner_user_id: str) -> bool:
    return is_the_owner(event.get("user"), owner_user_id)


def _owner_of(overlay: str, backend_for: Callable[[str], object]) -> str:
    try:
        return configured_owner_id(backend_for(overlay))
    except Exception:
        logger.exception("Could not read the owner of overlay %r; its rows are not owner work", overlay)
        return ""


def owner_rows(
    rows: "PendingChatInjectionQuerySet", backend_for: Callable[[str], object]
) -> "PendingChatInjectionQuerySet":
    """The rows in *rows* whose sender is the owner of the overlay that recorded them."""
    overlays = rows.order_by().values_list("overlay", flat=True).distinct()
    return rows.sent_by_owners({overlay: _owner_of(overlay, backend_for) for overlay in overlays})
