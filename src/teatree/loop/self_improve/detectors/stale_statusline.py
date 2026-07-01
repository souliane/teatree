"""``StaleStatuslineEntryDetector`` — last-rendered statusline references stale state.

Reads the last statusline file (``teatree.loop.statusline.default_path``)
and walks its lines looking for PR/issue references whose recorded
state has changed (closed/merged) since the render.  The detector is
the **only** Phase 1 detector with ``auto_fix=True``: re-rendering the
statusline is idempotent and side-effect-free, so an immediate self-heal
is safe.

The detector compares the URLs found in the statusline against a small
cache of "live" PR / Ticket state (the ``PullRequest`` model for PR
URLs, ``Ticket`` for issue URLs).  A URL appearing in the statusline
text whose corresponding PR is ``merged`` (or whose ticket is in a
terminal state) is the smell.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from teatree.core.models.pull_request import PullRequest
from teatree.core.models.ticket import Ticket
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.self_improve.dedup import canonical_key, state_hash
from teatree.loop.self_improve.detectors.base import ActionRung, DetectorReport
from teatree.loop.statusline_render import default_path

# Generic URL extractor — covers GitHub/GitLab PR + issue URLs in the
# statusline text (the OSC8 hyperlink wrapper still contains the URL in
# plain form between the escape sequences).
_URL_RE = re.compile(r"https?://[^\s\x1b\\]+")

_TERMINAL_TICKET_STATES = frozenset({Ticket.State.MERGED, Ticket.State.DELIVERED, Ticket.State.IGNORED})


def _default_statusline_reader() -> str:
    path = default_path()
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _default_rerender() -> None:
    """Sentinel default for a directly-constructed detector — never the real heal.

    The detector lives in the ``domain`` layer; an actual re-render composes the
    live-loop / open-PR / t3-master anchors, which live in the ``orchestration``
    layer (``teatree.loop.phases.render.rerender_statusline``). Reaching up to it
    from here would invert the tach-enforced dependency DAG, so the orchestration
    caller injects the real seam as the action-ladder ``auto_fix_callable``
    (``teatree.loop.phases.render.self_improve_rerender``) — retiring the prior
    no-op stub (#2625). This sentinel keeps a directly-constructed detector from
    crashing when nothing injected a callable.
    """
    return


@dataclass(slots=True)
class StaleStatuslineEntryDetector:
    """A merged/closed URL still appears in the last statusline render."""

    name: ClassVar[str] = "stale_statusline_entry"
    tier: ClassVar[str] = "cheap"
    severity: ClassVar[str] = "info"
    max_rung: ClassVar[str] = ActionRung.AUTO_FIX
    auto_fix: ClassVar[bool] = True

    statusline_reader: Callable[[], str] = field(default=_default_statusline_reader)
    rerender: Callable[[], None] = field(default=_default_rerender)
    statusline_path_resolver: Callable[[], Path] = field(default=default_path)

    def _stale_urls(self) -> list[tuple[str, str]]:
        """Return ``(url, reason)`` pairs for URLs whose state moved."""
        text = self.statusline_reader()
        if not text:
            return []
        urls = set(_URL_RE.findall(text))
        if not urls:
            return []
        stale: list[tuple[str, str]] = []
        pr_states = {pr.url: pr.state for pr in PullRequest.objects.filter(url__in=urls)}
        for url, pr_state in pr_states.items():
            if pr_state == PullRequest.State.MERGED:
                stale.append((url, f"pr_state={pr_state}"))
        ticket_states = {t.issue_url: t.state for t in Ticket.objects.filter(issue_url__in=urls)}
        for url, ticket_state in ticket_states.items():
            if ticket_state in _TERMINAL_TICKET_STATES:
                stale.append((url, f"ticket_state={ticket_state}"))
        return stale

    def detect(self) -> list[DetectorReport]:
        stale_pairs = self._stale_urls()
        if not stale_pairs:
            return []
        urls = sorted({url for url, _ in stale_pairs})
        reasons = sorted({reason for _, reason in stale_pairs})
        identity = ",".join(urls)
        return [
            DetectorReport(
                detector=self.name,
                dedup_key=canonical_key(self.name, "render"),
                state_hash=state_hash(identity, *reasons),
                severity=self.severity,
                max_rung=self.max_rung,
                summary=f"{len(urls)} stale URL(s) on statusline",
                payload={"urls": urls, "reasons": reasons},
                auto_fix=self.auto_fix,
            )
        ]

    def scan(self) -> list[ScanSignal]:
        return [report.to_signal() for report in self.detect()]
