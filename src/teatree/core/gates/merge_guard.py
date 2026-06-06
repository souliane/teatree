"""Overlay merge-guard verdict (#654).

``MergeGuard`` is the return type of ``OverlayBase.can_auto_merge``.  It is an
immutable value object — overlays construct one and return it; the scanner reads
it and acts.

Semantics
---------
* ``allowed=True``  → proceed with the automatic merge signal (default).
* ``allowed=False, escalate=False`` → record a blocked signal; no merge action.
* ``allowed=False, escalate=True``  → emit an escalation signal so the operator
    can intervene.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MergeGuard:
    """Verdict returned by ``OverlayBase.can_auto_merge``."""

    allowed: bool
    reason: str = ""
    escalate: bool = False

    @classmethod
    def allow(cls) -> "MergeGuard":
        """Convenience constructor for the permissive (allowed) case."""
        return cls(allowed=True)
