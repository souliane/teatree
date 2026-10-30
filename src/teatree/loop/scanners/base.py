"""Scanner protocol + the structured ``ScanSignal`` record.

Each scanner returns a list of ``ScanSignal``s. The dispatcher reads the
``kind`` field to decide whether to act inline (fix-and-push, statusline
note, webhook trigger) or hand off to a phase agent.
"""

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx

from teatree import forge_credentials
from teatree.backends.github.api import run_gh_unchecked
from teatree.forge_credentials import ForgeTokenResolution, ForgeTokenState
from teatree.types import ScannerError, ScannerErrorClass
from teatree.utils.run import CompletedProcess

__all__ = [
    "PartialScanError",
    "ScanSignal",
    "Scanner",
    "ScannerError",
    "ScannerErrorClass",
    "SignalPayload",
    "classify_gh_stderr",
    "classify_http_error",
    "hours_since",
    "run_gh",
    "run_gh_for_slug",
]

type SignalPayload = dict[str, Any]


def hours_since(earlier: dt.datetime, *, now: dt.datetime) -> float:
    """Fractional hours from *earlier* to *now*.

    The shared cadence-gate primitive for the once-per-N-hours scanners
    (``backlog_sweep``, ``eval_local``, ``provision_smoke``,
    ``architectural_review``, ``scanning_news``): each guards its own never-run
    ``bootstrap`` case with a plain ``last_run_at is None`` check — which also
    narrows the nullable ``Max("session__started_at")`` aggregate to a concrete
    ``datetime`` — then calls this for the elapsed measurement. That replaces
    five copy-pasted ``(now - earlier).total_seconds() / 3600.0`` sites, each of
    which carried a ``# type: ignore[operator]`` only because the un-narrowed
    ``object``-typed operand defeated the subtraction's type check.
    """
    return (now - earlier).total_seconds() / 3600.0


def recent_iso_stamp(value: object, *, now: dt.datetime, window: dt.timedelta) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return dt.datetime.fromisoformat(value) > now - window
    except (TypeError, ValueError):
        return False


def classify_gh_stderr(stderr: str) -> ScannerErrorClass:
    """Classify a non-zero ``gh`` stderr into a :class:`ScannerErrorClass` (#1287).

    The classifier reads gh's well-known error wording: auth-required
    prompts (``gh auth login``, ``GH_TOKEN``, ``Bad credentials``, ``401``),
    GitHub rate-limit messages (``rate limit``), pooled ``gh api`` headers
    (``remaining=0``, ``retry-after=`` on HTTP 403/429), and network failures (``dial
    tcp``, ``no such host``, ``Could not resolve``). Anything else falls
    through to :attr:`ScannerErrorClass.UNKNOWN` so the dispatcher still
    surfaces the failure rather than masking it.

    Shared by every ``gh``-backed scanner (pr_sweep, codex_review, …) so
    the marker lists stay in one place.
    """
    lower = stderr.lower()
    rate_limit_markers = ("rate limit", "rate-limit", "secondary rate")
    auth_markers = ("gh auth login", "gh_token", "bad credentials")
    network_markers = ("no such host", "could not resolve", "dial tcp", "network is unreachable")
    status = re.search(r"\bhttp ([1-5]\d\d)\b", lower)
    rate_limited_status = status is not None and status.group(1) in {"403", "429"}
    if any(marker in lower for marker in rate_limit_markers) or (
        rate_limited_status and ("remaining=0" in lower or "retry-after=" in lower)
    ):
        return ScannerErrorClass.RATE_LIMIT
    auth_text = re.sub(r"\b(?:remaining|retry-after)=\d+\b", "", lower)
    if any(marker in auth_text for marker in auth_markers) or re.search(r"(?<![\w:])401(?!\w|:\w)", auth_text):
        return ScannerErrorClass.AUTH
    if any(marker in lower for marker in network_markers):
        return ScannerErrorClass.NETWORK
    return ScannerErrorClass.UNKNOWN


def classify_http_error(exc: Exception) -> ScannerErrorClass:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in {httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN}:
            return ScannerErrorClass.AUTH
        if status == httpx.codes.TOO_MANY_REQUESTS:
            return ScannerErrorClass.RATE_LIMIT
        return ScannerErrorClass.UNKNOWN
    if isinstance(exc, httpx.HTTPError):
        return ScannerErrorClass.NETWORK
    return ScannerErrorClass.UNKNOWN


def run_gh(argv: list[str], *, resolution: ForgeTokenResolution) -> CompletedProcess[str]:
    if resolution.state is not ForgeTokenState.TOKEN or not resolution.token:
        detail = f"{resolution.setting} is {resolution.state.value}: {resolution.detail}"
        return CompletedProcess(
            args=argv, returncode=4, stdout="", stderr=f"{detail}; refusing ambient gh authentication"
        )
    return run_gh_unchecked(argv, token=resolution.token)


def run_gh_for_slug(argv: list[str], slug: str) -> CompletedProcess[str]:
    resolution = forge_credentials.resolve_slug_token(slug, forge="github", credential="github_token")
    return run_gh(argv, resolution=resolution)


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


class PartialScanError(ScannerError):
    """A scanner failure that still hands over the signals read before it failed."""

    def __init__(self, *, scanner: str, detail: str, signals: list[ScanSignal]) -> None:
        super().__init__(scanner=scanner, error_class=ScannerErrorClass.UNKNOWN, detail=detail)
        self.signals = signals


@runtime_checkable
class Scanner(Protocol):
    """A pure-Python scanner that produces signals during one tick."""

    name: str

    def scan(self) -> list[ScanSignal]: ...  # pragma: no branch
