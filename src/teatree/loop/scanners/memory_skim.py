"""Weekly skim of the Claude memory corpus for entries that belong in the repo.

Directive 32 (a restatement of directives 6 and 2): each week, anything in the
personal Claude memories that concerns the factory's behaviour must be promoted
into the teatree repo — code or skills — and only memories with no bearing on
teatree's behaviour may stay; when the call is unclear, ask the owner what to
promote and what to drop.

The classifier for "this memory is a factory guardrail, not a personal
preference" already existed as :mod:`teatree.memory_audit` behind the manual
``t3 tool audit-memory`` command. Nothing ran it on a cadence, so the promotable
set only ever grew. This scanner is the weekly caller, and the decision the
directive reserves for the owner is its deliverable: ONE durable
:class:`~teatree.core.models.DeferredQuestion` per ISO week naming every
promotable memory and asking which to promote and which to drop. The question
rides the ordinary ask-gate, so the backlog nag resurfaces it until it is
answered rather than the skim silently repeating.

The ISO week is the dedupe scope, checked against every row rather than only the
pending ones: a week whose question was already answered must not be re-asked by
the next tick in that same week.
"""

import datetime as dt
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import OperationalError, ProgrammingError
from django.utils import timezone

from teatree.loop.scanners.base import ScanSignal

if TYPE_CHECKING:
    from teatree.memory_audit import MemoryEntry

logger = logging.getLogger(__name__)

#: How many memories the question names before it degrades to a "+N more" count.
_QUESTION_LIST_CAP = 20


def _week_marker(now: dt.datetime | None = None) -> str:
    year, week, _ = (now or timezone.now()).isocalendar()
    return f"memory-skim:{year}-W{week:02d}"


def skim_question_text(entries: "list[MemoryEntry]") -> str:
    """The owner-facing promote-or-drop question covering *entries*."""
    noun = "memories" if len(entries) != 1 else "memory"
    lines = [
        f"*Weekly memory skim — {len(entries)} personal {noun} read as factory behaviour.*",
        (
            "Anything that shapes how teatree behaves belongs in the repo (code or skills), "
            "not in personal memory. Which of these do you want me to promote, and which to drop?"
        ),
    ]
    lines.extend(f"  • {entry.name} → t3:{entry.suggested_skill}" for entry in entries[:_QUESTION_LIST_CAP])
    remainder = len(entries) - _QUESTION_LIST_CAP
    if remainder > 0:
        lines.append(f"  +{remainder} more.")
    return "\n".join(lines)


@dataclass(slots=True)
class MemorySkimScanner:
    name: str = "memory_skim"

    def scan(self) -> list[ScanSignal]:
        from teatree.core.models import DeferredQuestion  # noqa: PLC0415 — deferred: ORM needs the app registry
        from teatree.memory_audit import scan_all  # noqa: PLC0415 — deferred: loaded at tick time, not import

        marker = _week_marker()
        try:
            if DeferredQuestion.objects.filter(dedupe_marker=marker).exists():
                return []
            entries = scan_all()
            if not entries:
                return []
            question = DeferredQuestion.record(skim_question_text(entries), dedupe_marker=marker)
        except (OperationalError, ProgrammingError):
            logger.info("%s: DeferredQuestion unavailable (DB not migrated yet) — skipping", self.name)
            return []
        except Exception:
            logger.exception("%s skim failed", self.name)
            return []
        noun = "memories" if len(entries) != 1 else "memory"
        return [
            ScanSignal(
                kind="memory.skim_promotable",
                summary=f"{len(entries)} {noun} queued for promote-or-drop",
                payload={"promotable": len(entries), "question_id": question.pk},
            ),
        ]
