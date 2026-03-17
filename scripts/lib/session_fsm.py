"""Session phase state machine with quality gates.

Tracks what the agent is doing in the current conversation. Ephemeral —
resets each session. Quality gates prevent unsafe transitions (e.g.,
shipping without testing).

States: idle → scoping → coding → testing → debugging → reviewing →
        shipping → requesting_review → retrospecting
"""

import json
from pathlib import Path
from typing import Any


class GateBlockedError(Exception):
    """Raised when a quality gate blocks a transition.

    Use --force (with explicit user approval) to override.
    """


class SessionPhase:
    """Tracks agent session phase with quality gates."""

    state: str = "idle"

    def __init__(self, session_id: str, state_dir: str) -> None:
        self.session_id = session_id
        self.state_dir = state_dir
        self.visited: set[str] = {"idle"}
        self._load()

    def _state_file(self) -> Path:
        return Path(self.state_dir) / f"{self.session_id}.session.json"

    def _load(self) -> None:
        sf = self._state_file()
        if sf.is_file():
            data = json.loads(sf.read_text(encoding="utf-8"))
            self.state = data.get("state", "idle")
            self.visited = set(data.get("visited", ["idle"]))

    def save(self) -> None:
        sf = self._state_file()
        Path(self.state_dir).mkdir(parents=True, exist_ok=True)
        data = {
            "state": self.state,
            "visited": sorted(self.visited),
        }
        sf.write_text(
            json.dumps(data, indent=2) + "\n",
            encoding="utf-8",
        )

    def has_visited(self, phase: str) -> bool:
        return phase in self.visited

    def _transition(self, target: str) -> None:
        self.visited.add(target)
        self.state = target
        self.save()

    def _check_gate(self, target: str, required_phases: list[str], *, force: bool = False) -> None:
        """Check that required phases have been visited before transitioning.

        Raises GateBlockedError if any required phase was not visited,
        unless force=True.
        """
        if force:
            return
        missing = [p for p in required_phases if not self.has_visited(p)]
        if missing:
            msg = (
                f"Cannot transition to {target}: "
                f"session has not passed {', '.join(missing)}. "
                f"Use --force to override (requires explicit user approval)."
            )
            raise GateBlockedError(msg)

    def available_transitions(self) -> list[dict[str, Any]]:
        """Return transitions available from the current state."""
        result: list[dict[str, Any]] = []
        for method_name, allowed_from, required, target in _TRANSITION_TABLE:
            if "*" in allowed_from or self.state in allowed_from:
                missing = [p for p in required if not self.has_visited(p)]
                entry: dict[str, Any] = {"method": method_name, "target": target}
                if missing:
                    entry["blocked"] = True
                    entry["missing"] = missing
                result.append(entry)
        return result

    # --- Transitions ---

    def begin_scoping(self, *, force: bool = False) -> None:  # noqa: ARG002
        self._transition("scoping")

    def begin_coding(self, *, force: bool = False) -> None:  # noqa: ARG002
        self._transition("coding")

    def begin_testing(self, *, force: bool = False) -> None:  # noqa: ARG002
        self._transition("testing")

    def begin_debugging(self, *, force: bool = False) -> None:  # noqa: ARG002
        self._transition("debugging")

    def begin_reviewing(self, *, force: bool = False) -> None:
        self._check_gate("reviewing", ["testing"], force=force)
        self._transition("reviewing")

    def begin_shipping(self, *, force: bool = False) -> None:
        self._check_gate("shipping", ["testing", "reviewing"], force=force)
        self._transition("shipping")

    def begin_requesting_review(self, *, force: bool = False) -> None:
        self._check_gate("requesting_review", ["shipping"], force=force)
        self._transition("requesting_review")

    def begin_retrospecting(self, *, force: bool = False) -> None:  # noqa: ARG002
        self._transition("retrospecting")


# Transition table for introspection: (method, allowed_from, required_phases, target)
_TRANSITION_TABLE: list[tuple[str, list[str], list[str], str]] = [
    ("begin_scoping", ["idle"], [], "scoping"),
    ("begin_coding", ["*"], [], "coding"),
    ("begin_testing", ["*"], [], "testing"),
    ("begin_debugging", ["*"], [], "debugging"),
    ("begin_reviewing", ["*"], ["testing"], "reviewing"),
    ("begin_shipping", ["*"], ["testing", "reviewing"], "shipping"),
    ("begin_requesting_review", ["*"], ["shipping"], "requesting_review"),
    ("begin_retrospecting", ["*"], [], "retrospecting"),
]
