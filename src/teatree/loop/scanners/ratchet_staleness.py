"""Reference-ratchet staleness on the teatree core clone (#4451) — a pure trigger.

#4451's outage: two PRs off a shared base, one seeding a pin in the
``known_unresolved_refs.yaml`` ratchet while the other rewrote the citation it
named. Neither ``refs/pull/N/merge`` contained the other, so both were green
alone and ``main`` went red on six required contexts the moment the second
landed — with every open PR inheriting it. Nobody owned the fix for hours.

Diagnosis is now one command (``t3 tool ratchet-prune``); what remains is
NOTICING. This scanner reads the maintained core clone's tree and flags the
condition on the next tick instead of waiting for someone to read a CI log.

Detection is from the TREE, not from CI: the stale set is a pure function of the
clone's own baseline versus its own source, so the answer arrives without a forge
round-trip and without waiting for a pipeline. The scan is READ-ONLY — it never
writes to the clone, because a dirty working tree would make
:class:`~teatree.loop.scanners.pull_main_clone.PullMainCloneScanner` skip its
fast-forward, trading one stale artifact for another.

Observe-only, mirroring :mod:`teatree.loop.scanners.ci_eval_heal` (which flags
open sessions and never fixes them). Opening the repair PR by itself would be the
sibling half — the shape ``ci_eval_heal`` puts behind its own dark
``ci_eval_heal_autofix_enabled`` flag — and is deliberately not built here.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from teatree.loop.scanners.base import ScanSignal
from teatree.quality.ref_baseline import BaselineError, stale_entries

logger = logging.getLogger(__name__)

#: The single signal kind this scanner emits — routed to the
#: ``report_ratchet_staleness`` mechanical handler via ``MECHANICAL_BY_KIND``.
RATCHET_STALENESS_KIND = "ratchet.stale_pins"

_BASELINE_RELPATH = Path("src") / "teatree" / "quality" / "known_unresolved_refs.yaml"


@dataclass(slots=True)
class RatchetStalenessScanner:
    """Emit one signal while the core clone carries stale ratchet pins; nothing when clean."""

    repo: Path
    name: str = "ratchet_staleness"

    def scan(self) -> list[ScanSignal]:
        baseline = self.repo / _BASELINE_RELPATH
        if not baseline.is_file():
            # A clone older than #4451 has no baseline to be stale against.
            return []
        try:
            stale = stale_entries(self.repo, path=baseline)
        except BaselineError:
            # The read failed loud into the log; the tick must still finish, so this
            # caller absorbs it rather than aborting every other scanner behind it.
            logger.exception("%s: could not read %s", self.name, baseline)
            return []
        rows = sorted((ratchet, path, ref) for ratchet, pins in stale.items() for path, ref in pins)
        if not rows:
            return []
        logger.warning("%s: %d stale ratchet pin(s) on %s", self.name, len(rows), self.repo)
        return [
            ScanSignal(
                kind=RATCHET_STALENESS_KIND,
                summary=f"{len(rows)} stale reference-ratchet pin(s) on the core clone",
                payload={"repo": str(self.repo), "stale": [list(row) for row in rows]},
            )
        ]


__all__ = ["RATCHET_STALENESS_KIND", "RatchetStalenessScanner"]
