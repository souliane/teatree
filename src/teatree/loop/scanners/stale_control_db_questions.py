"""Reclaimable control-DB copies become ONE question, never a deletion (A8).

``find_control_db_artifacts`` names every file that is, or once was, a control database.
It had one consumer, and that consumer asks only who is HOLDING them open — so nothing
ever proposed reclaiming the space and it accumulated silently (measured on this box: 40
files, 489 MB, including two 71 MB copies of each other).

A8 fixes the shape rather than the number: the factory PROPOSES and the owner decides. So
this files a question listing what it found and deletes nothing, ever. B8's default is
KEEP, and a database copy is the artifact where a wrong delete is unrecoverable.

Same mechanism as the inert-gate ask, deliberately — one deduped ``DeferredQuestion`` under
a marker fingerprinting the artifact set, so a check running every tick asks once per set and
never again once the owner has answered or dismissed it.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.loop.scanners.base import ScanSignal

#: One marker per artifact SET, not per file: an unchanged set is asked once, a changed one again.
MARKER_PREFIX = "control-db-artifacts:"

#: How many paths the question names before it stops — enough to judge, short enough to read.
_NAMED = 8

_MIB = 1024 * 1024


def _artifacts() -> list[Path]:
    """Every reclaimable control-DB copy this venue can name — the seam a test re-points."""
    from teatree.paths import DATA_DIR, TRUE_CANONICAL_DB, find_control_db_artifacts  # noqa: PLC0415 — deferred

    return list(find_control_db_artifacts(DATA_DIR, canonical=TRUE_CANONICAL_DB))


def _question(paths: list[Path], *, total_bytes: int) -> str:
    named = ", ".join(path.name for path in paths[:_NAMED])
    more = f" and {len(paths) - _NAMED} more" if len(paths) > _NAMED else ""
    return (
        f"{len(paths)} file(s) beside the control database are copies or leftovers of it, "
        f"holding {total_bytes / _MIB:.0f} MiB: {named}{more}. None is the canonical database. "
        "Reclaim the space, or keep them?"
    )


@dataclass(slots=True)
class StaleControlDbQuestionScanner:
    """Propose reclaiming the control-DB leftovers, once, and never touch them."""

    #: Re-points the probe away from this venue's real data dir — what lets a test declare a
    #: set of leftovers and then assert every one of them is still on disk afterwards.
    artifacts: Callable[[], list[Path]] = field(default=_artifacts)
    name: str = "stale_control_db_questions"

    def scan(self) -> list[ScanSignal]:
        from teatree.core.models.deferred_question import DeferredQuestion  # noqa: PLC0415 — deferred: ORM
        from teatree.core.models.question_text import question_fingerprint  # noqa: PLC0415 — deferred: ORM

        paths = self.artifacts()
        total = sum(path.stat().st_size for path in paths)
        if not paths:
            return []

        question = _question(paths, total_bytes=total)
        names = sorted(path.name for path in paths)
        DeferredQuestion.record(
            question,
            dedupe_marker=f"{MARKER_PREFIX}{question_fingerprint(' '.join(names))}",
            decision=OwnerDecision.IRREVERSIBLE,
            checked=[f"find_control_db_artifacts lists {', '.join(names)}", f"{total} bytes, none the canonical DB"],
        )
        return [
            ScanSignal(
                kind="disk.reclaimable",
                summary=question,
                payload={"files": len(paths), "bytes": total},
            )
        ]


__all__ = ["MARKER_PREFIX", "StaleControlDbQuestionScanner"]
