"""Retry answered owner action cards on the global dispatch tick."""

import logging
from dataclasses import dataclass

from django.db import OperationalError, ProgrammingError

from teatree.core.owner_actions import carry_out_owner_actions
from teatree.loop.scanners.base import ScanSignal

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class OwnerActionScanner:
    name: str = "owner_actions"

    def scan(self) -> list[ScanSignal]:
        try:
            outcomes = carry_out_owner_actions()
        except (OperationalError, ProgrammingError):
            logger.info("OwnerActionScanner: question tables unavailable; skipping")
            return []
        return [
            ScanSignal(kind="owner_action.carried_out", summary=outcome, payload={"scanner": self.name})
            for outcome in outcomes
        ]
