"""Scanner protocol + the structured ``ScanSignal`` record.

Each scanner returns a list of ``ScanSignal``s. The dispatcher reads the
``kind`` field to decide whether to act inline (fix-and-push, statusline
note, webhook trigger) or hand off to a phase agent.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from teatree.types import ScannerError, ScannerErrorClass

__all__ = [
    "ScanSignal",
    "Scanner",
    "ScannerError",
    "ScannerErrorClass",
    "SignalPayload",
    "classify_gh_stderr",
]

type SignalPayload = dict[str, Any]


def classify_gh_stderr(stderr: str) -> ScannerErrorClass:
    """Classify a non-zero ``gh`` stderr into a :class:`ScannerErrorClass` (#1287).

    The classifier reads gh's well-known error wording: auth-required
    prompts (``gh auth login``, ``GH_TOKEN``, ``Bad credentials``, ``401``),
    GitHub rate-limit messages (``API rate limit exceeded``, ``rate
    limit``, ``secondary rate limit``), and network failures (``dial
    tcp``, ``no such host``, ``Could not resolve``). Anything else falls
    through to :attr:`ScannerErrorClass.UNKNOWN` so the dispatcher still
    surfaces the failure rather than masking it.

    Shared by every ``gh``-backed scanner (pr_sweep, codex_review, …) so
    the marker lists stay in one place.
    """
    lower = stderr.lower()
    rate_limit_markers = ("rate limit", "rate-limit", "secondary rate")
    auth_markers = ("gh auth login", "gh_token", "bad credentials", "401")
    network_markers = ("no such host", "could not resolve", "dial tcp", "network is unreachable")
    if any(marker in lower for marker in rate_limit_markers):
        return ScannerErrorClass.RATE_LIMIT
    if any(marker in lower for marker in auth_markers):
        return ScannerErrorClass.AUTH
    if any(marker in lower for marker in network_markers):
        return ScannerErrorClass.NETWORK
    return ScannerErrorClass.UNKNOWN


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
