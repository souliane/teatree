"""Ticket delivery lifecycle state machine.

States: not_started → scoped → started → coded → tested → reviewed →
        shipped → in_review → merged → delivered

Persists to ticket.json in the ticket directory.
"""

import json
from pathlib import Path
from typing import Any

from lib.fsm import get_available_transitions, transition

_STATE_FILENAME = "ticket.json"


class TicketLifecycle:
    """Tracks ticket delivery state with guarded transitions."""

    state: str = "not_started"

    def __init__(self, ticket_dir: str) -> None:
        self.ticket_dir = ticket_dir
        self.facts: dict[str, Any] = {}
        self._load()

    def _state_file(self) -> Path:
        return Path(self.ticket_dir) / _STATE_FILENAME

    def _load(self) -> None:
        sf = self._state_file()
        if sf.is_file():
            data = json.loads(sf.read_text(encoding="utf-8"))
            self.state = data.get("state", "not_started")
            self.facts = data.get("facts", {})

    def save(self) -> None:
        sf = self._state_file()
        if self.state == "not_started" and not self.facts:
            if sf.is_file():
                sf.unlink()
            return
        data = {"state": self.state, "facts": self.facts}
        sf.write_text(
            json.dumps(data, indent=2, default=str) + "\n",
            encoding="utf-8",
        )

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "ticket_dir": self.ticket_dir,
            "facts": self.facts,
            "available_transitions": self.available_transitions(),
        }

    def available_transitions(self) -> list[dict[str, Any]]:
        return [
            {
                "method": t["method"],
                "conditions": [c.__name__ for c in t["conditions"]],
            }
            for t in get_available_transitions(self)
        ]

    # --- Forward transitions (happy path) ---

    @transition(source="not_started", target="scoped")
    def scope(self, issue_url: str) -> None:
        self.facts["issue_url"] = issue_url

    @transition(source="scoped", target="started")
    def start(self, worktree_dirs: list[str]) -> None:
        self.facts["worktree_dirs"] = worktree_dirs

    @transition(source="started", target="coded")
    def code(self) -> None:
        pass

    @transition(source="coded", target="tested")
    def test(self, passed: bool = True) -> None:  # noqa: FBT002
        self.facts["tests_passed"] = passed

    @transition(source="tested", target="reviewed")
    def review(self) -> None:
        pass

    @transition(source="reviewed", target="shipped")
    def ship(self, mr_urls: list[str]) -> None:
        self.facts["mr_urls"] = mr_urls

    @transition(source="shipped", target="in_review")
    def request_review(self) -> None:
        pass

    @transition(source="in_review", target="merged")
    def mark_merged(self) -> None:
        pass

    @transition(source="merged", target="delivered")
    def mark_delivered(self) -> None:
        pass

    # --- Backward transitions (rework loops) ---

    @transition(source=["coded", "tested", "reviewed"], target="started")
    def rework(self) -> None:
        """Go back to started state for rework."""
        # Clear quality gate facts so they must be re-earned
        self.facts.pop("tests_passed", None)
