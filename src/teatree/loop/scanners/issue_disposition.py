"""Codify open-issue triage: auto-close only high-confidence DEAD issues (#2122).

The repository-scoped :class:`IssueDispositionScanner` unconditionally lists open issues carrying
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
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.forge_readback import issue_number
from teatree.loop.scanners.needs_triage_query import _issue_body, _issue_title, _issue_url, needs_triage_issues
from teatree.types import RawAPIDict

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket

logger = logging.getLogger(__name__)

CLOSE_CANDIDATE_KIND = "issue_disposition.close_candidate"

_LIVE_TICKET_STATES: frozenset[str] = frozenset(
    {"not_started", "scoped", "started", "planned", "coded", "tested", "reviewed", "in_review", "shipped"}
)

_WHITESPACE_RE = re.compile(r"\s+")
_PATH_TOKEN_RE = re.compile(r"`([^`]+)`")
_PATHLIKE_RE = re.compile(r"^[\w.\-/]+/[\w.\-/]+\.\w+$")


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
    duplicate_of: str = ""

    def to_signal(self, *, overlay: str) -> ScanSignal:
        payload = {"url": self.url, "reason": self.reason, "overlay": overlay}
        if self.duplicate_of:
            payload["duplicate_of"] = self.duplicate_of
        return ScanSignal(
            kind=CLOSE_CANDIDATE_KIND,
            summary=f"Auto-close DEAD issue ({self.reason}): {self.title}",
            payload=payload,
        )


@dataclass(slots=True)
class IssueDispositionScanner:
    """Emit close-candidate signals for high-confidence DEAD ``needs-triage`` issues.

    The whole scanner is gated default-OFF one layer up (see
    :func:`teatree.loop.scanner_factories._issue_disposition_scanner_for`): with
    a non-canonical overlay, no scanner is built, so this never runs.
    Each tick lists the operator's ``needs-triage`` issues, classifies each with
    the three deterministic buckets, and emits at most *max_closes_per_tick*
    candidates. ``path_exists`` is the injectable obsolescence oracle, asked about a
    path in the candidate's OWN repo and answering ``None`` when that repo cannot be
    judged; leaving it ``None`` disables the ``obsolete`` bucket.
    """

    host: CodeHostBackend
    repo_slugs: tuple[str, ...] | None = None
    overlay_name: str = ""
    identities: tuple[str, ...] = field(default_factory=tuple)
    #: Bounds ONE pass, so an auto-close run stays small enough to read afterwards.
    max_closes_per_tick: int = 5
    path_exists: Callable[[str, str], bool | None] | None = None
    name: str = "issue_disposition"

    def scan(self) -> list[ScanSignal]:
        assignees = self._resolve_identities()
        if not assignees or self.repo_slugs == ():
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
        if shipped := self._already_shipped_reason(url):
            return CloseCandidate(url=url, title=title, reason=shipped)
        if survivor := self._duplicate_survivor(url, title):
            return CloseCandidate(url=url, title=title, reason="exact_duplicate", duplicate_of=survivor)
        obsolete = self._obsolete_reason(url, _issue_body(issue))
        return CloseCandidate(url=url, title=title, reason=obsolete) if obsolete else None

    @staticmethod
    def _already_shipped_reason(url: str) -> str:
        ticket_model = cast("type[Ticket]", apps.get_model("core", "Ticket"))
        states = set(ticket_model.objects.filter(issue_url=url).values_list("state", flat=True))
        if states & _LIVE_TICKET_STATES:
            return ""
        return "already_shipped" if states & ticket_model.merged_states() else ""

    def _duplicate_survivor(self, url: str, title: str) -> str:
        """The open issue *url* duplicates, or ``""`` when *url* is the one its group keeps.

        Every member of an exact-title group sees the others, so each would otherwise
        call itself the duplicate and one batch would close them all. The group keeps
        its lowest-numbered issue; a member whose number cannot be read leaves the
        survivor undecided, and nothing closes.

        The search is bounded to the candidate's OWN repo, derived from its URL. The
        listing spans every owned repo, so two repos can carry one title with no
        relationship at all — and the number the group is ordered by is only
        comparable within a single repo. An unresolvable repo returns no candidate.
        """
        fingerprint = title_fingerprint(title)
        own_number = issue_number(url)
        own_repo = self.host.repo_for_issue_url(url)
        if not fingerprint or not own_repo or not own_number:
            return ""
        older: list[tuple[int, str]] = []
        for other in self.host.search_open_issues(repo=own_repo, query=title):
            other_url = _issue_url(other)
            if not other_url or other_url == url or title_fingerprint(_issue_title(other)) != fingerprint:
                continue
            other_number = issue_number(other_url)
            if not other_number:
                return ""
            if int(other_number) < int(own_number):
                older.append((int(other_number), other_url))
        return min(older)[1] if older else ""

    def _obsolete_reason(self, url: str, body: str) -> str:
        """``obsolete`` only when every referenced path is KNOWN absent from the candidate's own repo."""
        if self.path_exists is None:
            return ""
        paths = referenced_paths(body)
        repo = self.host.repo_for_issue_url(url)
        if not paths or not repo:
            return ""
        return "obsolete" if all(self.path_exists(repo, path) is False for path in paths) else ""

    def _resolve_identities(self) -> tuple[str, ...]:
        if self.identities:
            return tuple(dict.fromkeys(self.identities))
        user = self.host.current_user()
        return (user,) if user else ()

    def _needs_triage_issues(self, assignees: tuple[str, ...]) -> list[RawAPIDict]:
        return needs_triage_issues(self.host, assignees, repo_slugs=self.repo_slugs or ())
