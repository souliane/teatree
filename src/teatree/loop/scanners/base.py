"""Scanner protocol + the structured ``ScanSignal`` record.

Each scanner returns a list of ``ScanSignal``s. The dispatcher reads the
``kind`` field to decide whether to act inline (fix-and-push, statusline
note, webhook trigger) or hand off to a phase agent.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

type SignalPayload = dict[str, Any]


@dataclass(frozen=True, slots=True)
class ScanSignal:
    """One observation surfaced by a scanner during a tick.

    ``kind`` is the dispatcher key — e.g. ``"my_pr.failed"`` routes to the
    inline failure handler, ``"reviewer_pr.new_sha"`` dispatches to the
    reviewer phase agent. ``payload`` carries the raw scanner data for the
    handler; ``summary`` is the one-line statusline-friendly description.
    """

    kind: str
    summary: str
    payload: SignalPayload = field(default_factory=dict)


@runtime_checkable
class Scanner(Protocol):
    """A pure-Python scanner that produces signals during one tick."""

    name: str

    def scan(self) -> list[ScanSignal]: ...  # pragma: no branch
