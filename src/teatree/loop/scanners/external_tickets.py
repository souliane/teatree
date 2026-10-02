"""Scan tickets from overlay databases not loaded in the current Django process.

TOML overlays with their own project directory keep a separate SQLite DB.
This scanner reads those DBs directly via raw SQLite (no Django ORM) so the
tick can surface their tickets alongside the primary overlay's.
"""

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

from django.apps import apps

from teatree.loop.scanners.base import ScanSignal

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket

logger = logging.getLogger(__name__)

# Placeholder count is filled per-scan from the enum-sourced excluded set.
_QUERY_TEMPLATE = (
    "SELECT id, state, issue_url, overlay FROM teatree_ticket WHERE state NOT IN ({placeholders}) ORDER BY id"
)


@dataclass(slots=True)
class ExternalTicketsScanner:
    overlay_name: str
    db_path: Path
    name: str = field(init=False)

    def __post_init__(self) -> None:
        self.name = "external_tickets"

    def scan(self) -> list[ScanSignal]:
        if not self.db_path.is_file():
            return []
        # Enum-sourced (not raw strings) so this raw-SQLite reader excludes exactly
        # the states the ORM in-flight queryset does. ``apps.get_model`` yields the
        # values only — no ORM query/connection — so the "reads external DBs
        # directly" contract holds; sorted for a stable placeholder order.
        ticket_model = cast("type[Ticket]", apps.get_model("core", "Ticket"))
        excluded = tuple(sorted(ticket_model.in_flight_excluded_states()))
        placeholders = ", ".join("?" for _ in excluded)
        query = _QUERY_TEMPLATE.format(placeholders=placeholders)
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            try:
                rows = conn.execute(query, excluded).fetchall()
            finally:
                conn.close()
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
            logger.warning("Cannot read %s for %s: %s", self.db_path, self.overlay_name, exc)
            return []
        return [
            ScanSignal(
                kind="ticket.active",
                summary=f"#{row[0]} {row[1]}",
                payload={
                    "ticket_id": row[0],
                    "ticket_number": str(row[0]),
                    "state": row[1],
                    "issue_url": row[2] or "",
                },
            )
            for row in rows
        ]
