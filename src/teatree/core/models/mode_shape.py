"""The ONE structural invariant a mode's loop mask must satisfy, whoever wrote it (B4).

**A mask that keeps the box WRITING must leave something that can free the space.**
``db_backup`` consumes disk on a cadence and never frees any; ``resource_pressure`` and
``idle_stack_reaper`` are what freed it on both of one day's out-of-memory emergencies.
A mask admitting the writer while both reclaim loops are quiet can only ever consume, and
it is reached exactly when an operator grabs a stop-everything posture mid-incident.

Everything else that lived here is gone by owner decision. The load-bearing tier — five
loops no mask could quiet — is deleted (B4): ``off`` now means off, and what it strands
(no Slack control path, no stack reaping) it reports rather than silently refuses. The
intake-without-delivery rule is unrepresentable under B1 totality: it existed because an
absent entry inherited ``Loop.enabled``, and there are no absent entries any more.

The rule is a property of the MASK rather than of one named mode, so it holds for an
operator-written mode as much as a shipped one, and it is PURE over the mask: a total
table names every loop, so nothing has to be resolved against a base column to judge it.
"""

from collections.abc import Mapping
from dataclasses import dataclass

#: The load-bearing subset that RECLAIMS disk — with every one quiet nothing frees space.
DISK_RECLAIM_LOOPS: tuple[str, ...] = ("idle_stack_reaper", "resource_pressure")

#: The loop that CONSUMES disk on a cadence: a backup pass writes, it never frees.
BACKUP_LOOP = "db_backup"


@dataclass(frozen=True, slots=True)
class BackupWithoutReclaim:
    """A mask that keeps writing backups with nothing left that can free the space."""

    quieted_reclaim: tuple[str, ...]

    @property
    def detail(self) -> str:
        remedy = " ".join(f"`t3 loop preset edit <mode> --set {loop}=on`" for loop in self.quieted_reclaim)
        return (
            f"keeps {BACKUP_LOOP} admitted while every reclaim loop is quiet "
            f"({', '.join(self.quieted_reclaim)}) — the box goes on writing backups with nothing that can free "
            f"the space, so this mask can only ever consume disk. Admit the reclaim loops ({remedy}), or mask "
            f"{BACKUP_LOOP} off too."
        )


def backup_without_reclaim(entries: Mapping[str, object]) -> BackupWithoutReclaim | None:
    """The consume-without-relief shape in *entries*, or ``None`` when something can free disk."""
    if entries.get(BACKUP_LOOP) is not True:
        return None
    quieted = tuple(loop for loop in DISK_RECLAIM_LOOPS if entries.get(loop) is not True)
    if len(quieted) < len(DISK_RECLAIM_LOOPS):
        return None
    return BackupWithoutReclaim(quieted_reclaim=quieted)
