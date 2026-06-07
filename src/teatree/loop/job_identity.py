"""Leaf job-identity types for the loop tick fan-out.

``_ScannerJob`` (scanner + overlay tag), the ``Domain`` partition enum, the
``PER_OVERLAY_DOMAINS`` summary, and the canonical-overlay anchor. The smallest,
stable layer the scanner-builders and mini-loops depend DOWN on. Carved out of
the loop tick fan-out to stay under the module-health LOC cap.
"""

from dataclasses import dataclass
from enum import StrEnum

from teatree.loop.scanners import Scanner

_TUPLE_PAIR = 2


@dataclass(frozen=True, slots=True)
class _ScannerJob:
    """Internal record pairing a scanner with its overlay tag."""

    scanner: Scanner
    overlay: str


class Domain(StrEnum):
    """A partition of the per-tick scanner fan-out (#1482).

    The per-overlay members own disjoint, exhaustive slices of
    :func:`_jobs_for_overlay_backend`; :data:`PER_OVERLAY_DOMAINS`
    summed reproduces it exactly. ``DISPATCH`` is the global
    (non-overlay) dispatch set ``build_default_jobs`` hard-codes — it is the
    one member excluded from the per-overlay sum.
    """

    TICKETS = "tickets"
    SHIP = "ship"
    REVIEW = "review"
    FOLLOWUP = "followup"
    INBOX = "inbox"
    ARCH_REVIEW = "arch_review"
    AUDIT = "audit"
    HOUSEKEEPING = "housekeeping"
    ISSUE_IMPLEMENTER = "issue_implementer"
    ISSUE_DISPOSITION = "issue_disposition"
    DISPATCH = "dispatch"


PER_OVERLAY_DOMAINS: tuple[Domain, ...] = (
    Domain.TICKETS,
    Domain.SHIP,
    Domain.REVIEW,
    Domain.FOLLOWUP,
    Domain.INBOX,
    Domain.ARCH_REVIEW,
    Domain.AUDIT,
    Domain.HOUSEKEEPING,
    Domain.ISSUE_IMPLEMENTER,
    Domain.ISSUE_DISPOSITION,
)


_CANONICAL_CORE_OVERLAY = "t3-teatree"


_TUPLE_PAIR = 2
