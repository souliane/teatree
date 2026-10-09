"""Slack cursor-pagination helper shared across the ``backends.slack`` reads.

Slack's ``conversations.*`` / ``users.*`` reads return at most one page and
carry the next page's cursor in ``response_metadata.next_cursor``. This is the
single source of truth for extracting that cursor so every read in the package
walks pages the same way (#3507).
"""

from typing import cast

from teatree.types import RawAPIDict


def next_cursor(data: RawAPIDict) -> str | None:
    """The next pagination cursor, or ``None`` when there is no further page."""
    meta = data.get("response_metadata")
    if not isinstance(meta, dict):
        return None
    cursor = cast("RawAPIDict", meta).get("next_cursor")
    return cursor if isinstance(cursor, str) and cursor else None


__all__ = ["next_cursor"]
