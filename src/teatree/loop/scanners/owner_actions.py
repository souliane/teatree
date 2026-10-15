"""Retry answered owner action cards on the global dispatch tick."""

from dataclasses import dataclass

from teatree.core.owner_actions import carry_out_owner_actions
from teatree.loop.scanners.base import ScanSignal


@dataclass(slots=True)
class OwnerActionScanner:
    name: str = "owner_actions"

    def scan(self) -> list[ScanSignal]:
        return [
            ScanSignal(kind="owner_action.result", summary=outcome, payload={"scanner": self.name})
            for outcome in carry_out_owner_actions()
        ]
