"""Worktree lifecycle state machine.

States: created → provisioned → services_up → ready
Persists to .state.json in the ticket directory.
"""

import json
import re
from pathlib import Path
from typing import Any

from lib import registry
from lib.db import db_exists, worktree_db_name
from lib.env import find_free_ports
from lib.fsm import get_available_transitions, transition

_STATE_FILENAME = ".state.json"


class WorktreeLifecycle:
    """Tracks worktree lifecycle state with guarded transitions."""

    state: str = "created"

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
            self.state = data.get("state", "created")
            self.facts = data.get("facts", {})

    def save(self) -> None:
        sf = self._state_file()
        if self.state == "created" and not self.facts:
            # Clean slate — no file needed. Remove if exists.
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
            "available_transitions": [
                {
                    "method": t["method"],
                    "conditions": [c.__name__ for c in t["conditions"]],
                }
                for t in get_available_transitions(self)
            ],
        }

    @transition(source="created", target="provisioned")
    def provision(self, wt_dir: str, main_repo: str, variant: str) -> None:
        be, fe, pg, rd = find_free_ports(self.ticket_dir)
        self.facts["ports"] = {"backend": be, "frontend": fe, "postgres": pg, "redis": rd}
        self.facts["wt_dir"] = wt_dir
        self.facts["main_repo"] = main_repo
        self.facts["variant"] = variant

        # Delegate to extension points (same as wt_setup.py)
        registry.call("wt_symlinks", wt_dir, main_repo, variant)
        registry.call("wt_services", main_repo, wt_dir)

        match = re.search(r"\d+", Path(self.ticket_dir).name)
        ticket_number = match.group() if match else "0"
        db_name = worktree_db_name(ticket_number, variant)
        self.facts["db_name"] = db_name

        if not db_exists(db_name):
            registry.call("wt_db_import", db_name, variant, main_repo)
        registry.call("wt_post_db", wt_dir)

    @transition(source="provisioned", target="services_up")
    def start_services(self) -> None:
        wt_dir = self.facts.get("wt_dir", "")
        registry.call("wt_run_backend", wt_dir)
        registry.call("wt_run_frontend", wt_dir)

    @transition(source="services_up", target="ready")
    def verify(self) -> None:
        ports = self.facts.get("ports", {})
        self.facts["urls"] = {
            "backend": f"http://localhost:{ports.get('backend', 0)}",
            "frontend": f"http://localhost:{ports.get('frontend', 0)}",
        }

    @transition(source=["provisioned", "services_up", "ready"], target="provisioned")
    def db_refresh(self) -> None:
        db_name = self.facts.get("db_name", "")
        variant = self.facts.get("variant", "")
        main_repo = self.facts.get("main_repo", "")
        wt_dir = self.facts.get("wt_dir", "")
        if db_name and main_repo:
            registry.call("wt_db_import", db_name, variant, main_repo)
            registry.call("wt_post_db", wt_dir)

    @transition(source="*", target="created")
    def teardown(self) -> None:
        self.facts = {}
        # save() is called by the decorator — it detects state=="created" + empty facts
        # and removes the state file instead of writing it.
