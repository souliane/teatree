"""Scan tickets from overlay databases not loaded in the current Django process.

TOML overlays with their own project directory keep a separate SQLite DB.
This scanner reads those DBs directly via raw SQLite (no Django ORM) so the
tick can surface their tickets alongside the primary overlay's.
"""

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from teatree.loop.scanners.base import ScanSignal

logger = logging.getLogger(__name__)

_TERMINAL_STATES = ("delivered", "review_posted", "ignored")
_PLACEHOLDERS = ", ".join("?" for _ in _TERMINAL_STATES)
_QUERY = f"SELECT id, state, issue_url, overlay FROM teatree_ticket WHERE state NOT IN ({_PLACEHOLDERS}) ORDER BY id"  # noqa: S608 — literal placeholder count, no user input


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
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            try:
                rows = conn.execute(_QUERY, _TERMINAL_STATES).fetchall()
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
