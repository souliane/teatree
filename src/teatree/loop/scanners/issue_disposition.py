"""Codify open-issue triage: auto-close only high-confidence DEAD issues (#2122).

The default-OFF :class:`IssueDispositionScanner` lists open issues carrying
:data:`~teatree.core.models.NEEDS_TRIAGE_LABEL` and emits a
``issue_disposition.close_candidate`` signal for the small set of issues that
carry *machine-checkable* DEAD evidence — never a guess. A re-tick re-emits the
same candidate; the mechanical close handler is idempotent (a no-op on an
already-closed issue), so re-emission does no harm.

Three deterministic dead-evidence buckets, each falsified by a single live fact.
``already_shipped``: a delivered/merged :class:`Ticket` already exists for the
issue URL — a *live in-flight* ticket (any pre-delivery state) FALSIFIES it, the
issue is being worked, not dead. ``exact_duplicate``: the issue's title
fingerprint matches another OPEN issue on the same repo — a *unique* fingerprint
FALSIFIES it. ``obsolete``: every repository file path the issue body references
is gone from disk — a single *still-existing* path FALSIFIES it, and an issue
that references no path is left untouched (no evidence is not dead evidence).

The conservative bar is load-bearing: ANY uncertainty yields NO candidate and
the ``needs-triage`` label is left in place for a human. This scanner is the
triage half of the burn-down doctrine — it can CLOSE dead noise but is
*physically unable* to enqueue work: it creates no :class:`Task`, claims no
:class:`ImplementedIssueMarker`, and emits no agent-routed signal. Done-signal
stays the open-BUG count, never an empty queue.
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from django.apps import apps

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import NEEDS_TRIAGE_LABEL
from teatree.loop.scanners.base import ScanSignal
from teatree.types import RawAPIDict

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket

logger = logging.getLogger(__name__)

CLOSE_CANDIDATE_KIND = "issue_disposition.close_candidate"

_DELIVERED_TICKET_STATES: frozenset[str] = frozenset({"merged", "delivered", "retrospected"})
_LIVE_TICKET_STATES: frozenset[str] = frozenset(
    {"not_started", "scoped", "started", "planned", "coded", "tested", "reviewed", "in_review", "shipped"}
)

_WHITESPACE_RE = re.compile(r"\s+")
_PATH_TOKEN_RE = re.compile(r"`([^`]+)`")
_PATHLIKE_RE = re.compile(r"^[\w.\-/]+/[\w.\-/]+\.\w+$")


def _issue_url(issue: RawAPIDict) -> str:
    for name in ("web_url", "html_url"):
        value = issue.get(name)
        if isinstance(value, str):
            return value
    return ""


def _issue_title(issue: RawAPIDict) -> str:
    title = issue.get("title")
    return title if isinstance(title, str) else ""


def _issue_body(issue: RawAPIDict) -> str:
    for name in ("body", "description"):
        value = issue.get(name)
        if isinstance(value, str):
            return value
    return ""


def _issue_labels(issue: RawAPIDict) -> list[str]:
    labels = issue.get("labels")
    if not isinstance(labels, list):
        return []
    out: list[str] = []
    for item in labels:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            name = cast("RawAPIDict", item).get("name")
            if isinstance(name, str):
                out.append(name)
    return out


def _issue_is_open(issue: RawAPIDict) -> bool:
    state = issue.get("state")
    return not (isinstance(state, str) and state.lower() == "closed")


def title_fingerprint(title: str) -> str:
    return _WHITESPACE_RE.sub(" ", title).strip().lower()


def referenced_paths(body: str) -> list[str]:
    """Backtick-wrapped tokens in *body* that look like repository file paths.

    A path-like token has at least one ``/`` and a file extension — enough to
    avoid matching prose code spans (``git push``) while catching
    ``src/teatree/foo.py``. The obsolescence check is conservative on top of
    this: an issue that references no path token is never proposed for close.
    """
    return [token for token in _PATH_TOKEN_RE.findall(body) if _PATHLIKE_RE.match(token)]


@dataclass(frozen=True, slots=True)
class CloseCandidate:
    url: str
    title: str
    reason: str

    def to_signal(self, *, overlay: str) -> ScanSignal:
        return ScanSignal(
            kind=CLOSE_CANDIDATE_KIND,
            summary=f"Auto-close DEAD issue ({self.reason}): {self.title}",
            payload={"url": self.url, "reason": self.reason, "overlay": overlay},
        )


@dataclass(slots=True)
class IssueDispositionScanner:
    """Emit close-candidate signals for high-confidence DEAD ``needs-triage`` issues.

    The whole scanner is gated default-OFF one layer up (see
    :func:`teatree.loop.scanner_factories._issue_disposition_scanner_for`): with
    ``auto_disposition_enabled = false`` no scanner is built, so this never runs.
    Each tick lists the operator's ``needs-triage`` issues, classifies each with
    the three deterministic buckets, and emits at most *max_closes_per_tick*
    candidates. ``path_exists`` is the injectable obsolescence oracle (a clone-
    relative resolver in production); leaving it ``None`` disables the
    ``obsolete`` bucket so the scanner never guesses a path it cannot resolve.
    """

    host: CodeHostBackend
    repo: str = ""
    overlay_name: str = ""
    identities: tuple[str, ...] = field(default_factory=tuple)
    max_closes_per_tick: int = 5
    path_exists: Callable[[str], bool] | None = None
    name: str = "issue_disposition"

    def scan(self) -> list[ScanSignal]:
        assignees = self._resolve_identities()
        if not assignees:
            return []
        signals: list[ScanSignal] = []
        for issue in self._needs_triage_issues(assignees):
            if len(signals) >= self.max_closes_per_tick:
                break
            url = _issue_url(issue)
            try:
                candidate = self._classify(issue)
            except Exception:
                logger.exception("IssueDispositionScanner failed on issue %s", url or "<unknown>")
                continue
            if candidate is not None:
                signals.append(candidate.to_signal(overlay=self.overlay_name))
        return signals

    def _classify(self, issue: RawAPIDict) -> CloseCandidate | None:
        """Return a candidate ONLY when a single bucket holds machine-checkable DEAD evidence.

        The buckets are checked in falsification order; the first that holds wins.
        ANY uncertainty (no evidence, or a live fact that falsifies the bucket)
        returns ``None`` — the conservative bar — so the ``needs-triage`` hold
        stays in place for a human.
        """
        url = _issue_url(issue)
        if not url:
            return None
        title = _issue_title(issue)
        reason = (
            self._already_shipped_reason(url)
            or self._exact_duplicate_reason(url, title)
            or self._obsolete_reason(_issue_body(issue))
        )
        return CloseCandidate(url=url, title=title, reason=reason) if reason else None

    @staticmethod
    def _already_shipped_reason(url: str) -> str:
        ticket_model = cast("type[Ticket]", apps.get_model("core", "Ticket"))
        states = set(ticket_model.objects.filter(issue_url=url).values_list("state", flat=True))
        if states & _LIVE_TICKET_STATES:
            return ""
        return "already_shipped" if states & _DELIVERED_TICKET_STATES else ""

    def _exact_duplicate_reason(self, url: str, title: str) -> str:
        fingerprint = title_fingerprint(title)
        if not fingerprint or not self.repo:
            return ""
        matches = self.host.search_open_issues(repo=self.repo, query=title)
        for other in matches:
            other_url = _issue_url(other)
            if other_url and other_url != url and title_fingerprint(_issue_title(other)) == fingerprint:
                return "exact_duplicate"
        return ""

    def _obsolete_reason(self, body: str) -> str:
        if self.path_exists is None:
            return ""
        paths = referenced_paths(body)
        if not paths:
            return ""
        return "obsolete" if not any(self.path_exists(path) for path in paths) else ""

    def _resolve_identities(self) -> tuple[str, ...]:
        if self.identities:
            return tuple(dict.fromkeys(self.identities))
        user = self.host.current_user()
        return (user,) if user else ()

    def _needs_triage_issues(self, assignees: tuple[str, ...]) -> list[RawAPIDict]:
        seen_urls: set[str] = set()
        issues: list[RawAPIDict] = []
        for assignee in assignees:
            for issue in self.host.list_assigned_issues(assignee=assignee):
                if not _issue_is_open(issue):
                    continue
                if NEEDS_TRIAGE_LABEL not in _issue_labels(issue):
                    continue
                url = _issue_url(issue)
                if url and url in seen_urls:
                    continue
                if url:
                    seen_urls.add(url)
                issues.append(issue)
        return issues
