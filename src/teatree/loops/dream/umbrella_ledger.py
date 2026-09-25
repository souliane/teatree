"""Dream promote = fix-and-merge: the durable umbrella-checkbox ledger (#2663, #4776).

The promote/compliance phases used to file a fresh ``needs-triage`` GitHub issue
per gap. Those piled up, because the issue scanner SKIPs ``needs-triage``. The
umbrella-checkbox ledger replaces that: every grounded gap gets a checkbox under ONE
standing umbrella issue (souliane/teatree#2663) that is reused daily and never closed,
keyed on a stable gap key so the same gap never double-adds
(:func:`upsert_gap_checkbox`). The umbrella body is plain markdown — a task-list whose
lines each carry an invisible ``<!-- dream-gap <key> -->`` marker for stable dedup,
mirroring the fingerprint markers the Pass-2/compliance filers already embed.

Scheduling the fix used to happen HERE too, one gap at a time
(:func:`~teatree.core.models.ticket.Ticket.schedule_coding` per gap) — one gap, one
ticket, one PR. #4776 deleted that fan-out: every promoting phase now COLLECTS its
gaps into one :class:`~teatree.loops.dream.batch_promote.PromotionBatch` and
:mod:`teatree.loops.dream.batch_promote` mints AT MOST ONE ticket per pass, reusing
the checkbox primitives below. This module stays the durable ledger + the
:func:`reconcile_merged_gaps` drain for gap-fix tickets scheduled under the OLD
per-gap scheme, so a ticket already in flight when this shipped keeps draining.

The forge writes go through a passed-in
:class:`~teatree.core.backend_protocols.CodeHostBackend`, so the whole flow is
testable without an LLM and without a live forge. A rendered title that would leak
a banned term / bare reference is WITHHELD — never written to the umbrella.
"""

import enum
import logging
import re
from dataclasses import dataclass

from django.utils import timezone

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import ConsolidatedMemory
from teatree.core.models.ticket import Ticket
from teatree.core.review.review_findings import find_bare_references
from teatree.core.send_proxy import OutboundBlockedError, forge_from_url, route_forge_write
from teatree.hooks import banned_terms_scanner

logger = logging.getLogger(__name__)

#: The stable marker embedded (invisibly) in each umbrella checkbox line, keyed on
#: the gap key, so a re-run upserts in place rather than appending a duplicate.
_GAP_MARKER_PREFIX = "dream-gap"

#: The ticket-``extra`` keys that link an in-flight gap-fix Ticket back to its gap
#: identity, the memory to retire on merge, and the umbrella to check.
_GAP_KEY = "dream_gap_key"
_CLUSTER_KEY = "dream_memory_cluster_key"
_UMBRELLA_KEY = "dream_umbrella_url"
#: Stamped on a gap-fix Ticket's ``extra`` once its merge has been reconciled (checkbox
#: checked + memory retired), so :func:`reconcile_merged_gaps` skips it on every later
#: pass instead of re-reading the forge for the same merged gap forever (F6.9).
_RECONCILED_KEY = "dream_gap_reconciled_at"
#: Set on a DUPLICATE gap ticket whose substance was folded into a host ticket: the
#: host's pk. The member is retired IGNORED and never reaches MERGED itself, so its
#: merge signal is the host's (#2663).
_FOLDED_INTO_KEY = "dream_gap_folded_into"


def elided_snippet(text: str, limit: int) -> str:
    """*text* rstripped to *limit* chars, plus an ellipsis iff that cut something.

    A bare ``text[:limit]`` reads identically whether *text* was exactly *limit*
    chars or ten times that — the reader cannot tell a title is a fragment. The
    ellipsis is the honest signal that more text exists beyond the snippet.
    """
    trimmed = text[:limit].rstrip()
    return trimmed if len(text) <= limit else f"{trimmed}…"


@dataclass(frozen=True, slots=True)
class GapSpec:
    """One grounded gap's identity for promotion to a fix-and-merge.

    ``gap_key`` is the stable umbrella-checkbox / scheduling dedup key; ``title`` is
    the rendered checkbox label (scanned for banned terms / bare refs before any
    write); ``cluster_key`` links the gap-fix Ticket back to the ``ConsolidatedMemory``
    row to retire on merge (equal to ``gap_key`` for a core gap; the
    ``compliance-recurrence-<rule_identity>`` key for a recurrence). ``detail`` is the
    full, untruncated rule text for the coder-facing batch manifest
    (:func:`~teatree.loops.dream.batch_promote._batch_context`) — ``title`` is
    elided to a checkbox-sized snippet, so the manifest renders ``detail`` in full.
    Empty when ``title`` is already complete (e.g. a compliance-recurrence template).
    """

    gap_key: str
    title: str
    cluster_key: str
    detail: str = ""


def _marker(gap_key: str) -> str:
    return f"<!-- {_GAP_MARKER_PREFIX} {gap_key} -->"


def render_checkbox_line(*, gap_key: str, title: str, checked: bool, ticket_url: str = "") -> str:
    """Render one umbrella checkbox line carrying the title and the stable marker.

    The ``<!-- dream-gap <key> -->`` marker is invisible in the rendered issue but
    is the durable dedup/lookup key. A ``ticket_url`` (the fix PR/issue) is rendered
    inline when known so a human skimming the umbrella can click through.
    """
    box = "[x]" if checked else "[ ]"
    link = f" ([fix]({ticket_url}))" if ticket_url else ""
    return f"- {box} {title.strip()}{link} {_marker(gap_key)}"


def _line_index(lines: list[str], gap_key: str) -> int:
    """The index of the umbrella line carrying *gap_key*'s marker, or ``-1``."""
    marker = _marker(gap_key)
    for i, line in enumerate(lines):
        if marker in line:
            return i
    return -1


def _scrubbed_update(host: CodeHostBackend, *, umbrella_url: str, body: str) -> bool:
    """Route an umbrella body through the shared forge-write seam, then write it.

    The public-repo leak gate + the #117 send-proxy audit fire BEFORE the backend
    call — the same seam the MCP tools and the dream memory-gap filer use, so the
    umbrella's ``update_issue`` writes are no longer unscrubbed. A leak/blocked
    verdict SKIPs the write (returns ``False``) rather than crashing the dream
    pass, mirroring :func:`_read_body`'s never-crash contract.
    """
    try:
        clean = route_forge_write(
            forge=forge_from_url(umbrella_url),
            repo=umbrella_url,
            text=body,
            action="dream_umbrella_update",
            target=umbrella_url,
        )
    except OutboundBlockedError:
        return False
    host.update_issue(issue_url=umbrella_url, body=clean)
    return True


def _read_body(host: CodeHostBackend, umbrella_url: str) -> str | None:
    """Re-read the umbrella body; ``None`` on an unreadable forge state (never raises)."""
    try:
        raw = host.get_issue(umbrella_url)
    except Exception:  # noqa: BLE001 — a forge hiccup must not crash the dream pass; keep, don't write.
        return None
    body = raw.get("body") or raw.get("description")
    return body if isinstance(body, str) else None


def gap_present(host: CodeHostBackend, *, umbrella_url: str, gap_key: str) -> bool | None:
    """Whether *gap_key* already rides the umbrella — read-only discovery, no write.

    Tri-state on purpose: ``None`` is UNKNOWN (an unreadable body), never "absent" and
    never "present", so a caller that short-circuits before the upsert cannot claim a
    gap rides an umbrella nobody could read.
    """
    body = _read_body(host, umbrella_url)
    if body is None:
        return None
    return _line_index(body.splitlines(), gap_key) != -1


def upsert_gap_checkbox(
    host: CodeHostBackend, *, umbrella_url: str, gap_key: str, title: str, ticket_url: str = ""
) -> bool:
    """Add an unchecked checkbox for *gap_key* under the umbrella, deduped by key.

    Re-reads the umbrella body, and — only when no line already carries this gap's
    marker — appends one checkbox line and writes the whole body back. A gap that is
    already present is a no-op (idempotent; no rewrite). An unreadable body keeps
    the umbrella untouched (a forge hiccup never crashes the pass). Returns True iff
    a NEW checkbox was appended.
    """
    body = _read_body(host, umbrella_url)
    if body is None:
        return False
    lines = body.splitlines()
    if _line_index(lines, gap_key) != -1:
        return False
    lines.append(render_checkbox_line(gap_key=gap_key, title=title, checked=False, ticket_url=ticket_url))
    return _scrubbed_update(host, umbrella_url=umbrella_url, body="\n".join(lines) + "\n")


class _GapCheckState(enum.Enum):
    """Whether a gap's umbrella box is checked after an attempt to check it.

    A bare "did I flip it" boolean cannot separate "already checked" from "I could not
    check it", and the reconciler needs exactly that distinction: the first means the
    gap is done, the second means a forge read/write did not land.
    """

    NEWLY_CHECKED = "newly_checked"
    ALREADY_CHECKED = "already_checked"
    UNCONFIRMED = "unconfirmed"

    @property
    def is_checked(self) -> bool:
        return self is not _GapCheckState.UNCONFIRMED


_CHECKED_BOX = re.compile(r"^- \[x\]", re.IGNORECASE)
_UNCHECKED_BOX = re.compile(r"^- \[ \]")


def _ensure_gap_checked(host: CodeHostBackend, *, umbrella_url: str, gap_key: str) -> _GapCheckState:
    """Check *gap_key*'s umbrella box if needed, reporting whether it IS checked now."""
    body = _read_body(host, umbrella_url)
    if body is None:
        return _GapCheckState.UNCONFIRMED
    lines = body.splitlines()
    index = _line_index(lines, gap_key)
    if index == -1:
        return _GapCheckState.UNCONFIRMED
    if _CHECKED_BOX.match(lines[index]):
        return _GapCheckState.ALREADY_CHECKED
    flipped = _UNCHECKED_BOX.sub("- [x]", lines[index], count=1)
    if flipped == lines[index]:
        return _GapCheckState.UNCONFIRMED
    lines[index] = flipped
    written = _scrubbed_update(host, umbrella_url=umbrella_url, body="\n".join(lines) + "\n")
    return _GapCheckState.NEWLY_CHECKED if written else _GapCheckState.UNCONFIRMED


def check_gap_checkbox(host: CodeHostBackend, *, umbrella_url: str, gap_key: str) -> bool:
    """Flip *gap_key*'s umbrella checkbox from unchecked to checked, idempotently.

    Re-reads the body, flips the ``- [ ]`` of the line carrying this gap's marker to
    ``- [x]``, and writes the whole body back. An already-checked or absent gap is a
    no-op (no rewrite). Returns True iff the box was newly checked.
    """
    return _ensure_gap_checked(host, umbrella_url=umbrella_url, gap_key=gap_key) is _GapCheckState.NEWLY_CHECKED


def _withholding_reason(safe_title: str) -> str:
    """Why *safe_title* must never be written to the umbrella, or ``""`` when it may be.

    Shared with :mod:`teatree.loops.dream.batch_promote`'s ``consider()`` — the
    banned-term / bare-reference gate every promoted title passes through, whichever
    scheduling path is minting the ticket.
    """
    banned = banned_terms_scanner.scan_text(safe_title)
    if banned is not None:
        return f"contains banned term '{banned}'"
    leaked = find_bare_references(safe_title)
    if leaked:
        return f"contains bare reference(s): {', '.join(leaked)}"
    return ""


def _in_flight_gap_tickets() -> list[Ticket]:
    """Every not-yet-reconciled Ticket scheduled to fix a dream gap (carrying the gap-key marker).

    Excludes tickets already stamped :data:`_RECONCILED_KEY`: once a merged gap has been
    reconciled (checkbox checked, memory retired) there is nothing left to do, so it is
    dropped from the scan rather than re-read from the forge every pass forever (F6.9).
    """
    return list(
        Ticket.objects.exclude(extra__dream_gap_key__isnull=True)
        .exclude(extra__dream_gap_key="")
        .filter(extra__dream_gap_reconciled_at__isnull=True)
    )


def _merged_pr_url(ticket: Ticket) -> str:
    """The merged PR URL backing this gap-fix ticket, or ``""`` when none merged."""
    from teatree.core.models.pull_request import PullRequest  # noqa: PLC0415 — deferred: ORM/app-registry

    pr = PullRequest.objects.filter(ticket=ticket, state=PullRequest.State.MERGED).first()
    return pr.url if pr is not None else ""


def _merge_bearing_ticket(ticket: Ticket) -> Ticket | None:
    """The ticket whose MERGED state settles *ticket*'s gap — itself, or its fold host.

    A duplicate gap is retired by folding it into the host that carries the one real fix,
    which leaves the member IGNORED: it never reaches MERGED, so reading its own state
    left its umbrella box open forever after the host's PR landed (#2663). ``None`` when
    the pointer names no ticket, which reconciles nothing rather than raising.
    """
    host_pk = (ticket.extra or {}).get(_FOLDED_INTO_KEY)
    if not host_pk:
        return ticket
    return Ticket.objects.filter(pk=host_pk).first()


def reconcile_merged_gaps(host: CodeHostBackend, *, umbrella_url: str) -> list[Ticket]:
    """Check the umbrella checkbox + retire the memory for every MERGED gap-fix Ticket.

    For each in-flight gap whose fix Ticket reached MERGED, CHECK its umbrella
    checkbox and stamp the linked ``ConsolidatedMemory``'s ``ticket_url`` to the
    merged PR, then retire the prose through the EXISTING
    :func:`~teatree.loops.dream.promote_memory.retire_resolved_memories` — driven off
    the Ticket's authoritative MERGED state (an injected ``is_resolved`` predicate),
    NOT a fragile forge re-read of a ``/pull/<n>`` URL the issue endpoint does not
    serve. A BINDING memory is never retired; a gap whose fix has not merged is left
    alone. Returns the gap-fix tickets reconciled this pass.

    A gap whose umbrella box could NOT be confirmed checked — an unreadable body, a
    missing line, a refused write — is left unstamped and retried next pass. The stamp
    is permanent (it removes the ticket from every future scan), so stamping on an
    unconfirmed forge write would leave the umbrella showing an open box for a merged
    fix, with nothing left to ever re-check it.
    """
    from teatree.loops.dream.promote_memory import retire_resolved_memories  # noqa: PLC0415 — tick-time import

    reconciled: list[Ticket] = []
    merged_memory_urls: set[str] = set()
    for ticket in _in_flight_gap_tickets():
        merge_bearer = _merge_bearing_ticket(ticket)
        if merge_bearer is None or merge_bearer.state != Ticket.State.MERGED:
            continue
        gap_key = str((ticket.extra or {}).get(_GAP_KEY) or "")
        cluster_key = str((ticket.extra or {}).get(_CLUSTER_KEY) or "")
        merged_url = _merged_pr_url(merge_bearer)
        if not _ensure_gap_checked(host, umbrella_url=umbrella_url, gap_key=gap_key).is_checked:
            logger.warning("dream reconcile: could not check umbrella box for gap %r — retrying next pass", gap_key)
            continue
        if _stamp_memory_merged(cluster_key, merged_url=merged_url):
            merged_memory_urls.add(merged_url)
        _stamp_ticket_reconciled(ticket)
        reconciled.append(ticket)
    retire_resolved_memories(host, is_resolved=lambda url: url in merged_memory_urls)
    return reconciled


def _stamp_ticket_reconciled(ticket: Ticket) -> None:
    """Mark a gap-fix Ticket reconciled so later passes skip it (F6.9).

    Records :data:`_RECONCILED_KEY` in the ticket's ``extra``; the next
    :func:`_in_flight_gap_tickets` scan excludes it, so a merged gap is reconciled once,
    not re-read from the forge on every pass forever. Idempotent — an already-stamped
    ticket is left untouched.

    The unstamped read is a fast path, not the guard: it keeps the common already-stamped
    case from taking the write lock at all. The write itself goes through
    :meth:`Ticket.merge_extra`, so a concurrent writer's keys survive instead of being
    clobbered by this snapshot. Two passes racing the first stamp both write, and the
    later timestamp wins — harmless, because what the scan reads is the key's presence.
    """
    if (ticket.extra or {}).get(_RECONCILED_KEY):
        return
    ticket.merge_extra(set_keys={_RECONCILED_KEY: timezone.now().isoformat()})


def _stamp_memory_merged(cluster_key: str, *, merged_url: str) -> bool:
    """Point the gap's memory at its merged fix so the existing retire path fires.

    The memory is advanced to TICKETED with the merged PR as its ``ticket_url`` (the
    reconcile then drives ``retire_resolved_memories`` off the authoritative MERGED
    signal). A row already TICKETED/retired or with no merged URL is left untouched.
    Returns True iff this stamped a row TICKETED with *merged_url*.
    """
    if not cluster_key or not merged_url:
        return False
    row = ConsolidatedMemory.objects.filter(cluster_key=cluster_key).first()
    if row is None:
        return False
    if row.disposition not in {
        ConsolidatedMemory.Disposition.UNTRIAGED,
        ConsolidatedMemory.Disposition.CORE_GAP_NEEDS_TICKET,
    }:
        return False
    if row.disposition == ConsolidatedMemory.Disposition.UNTRIAGED:
        row.classify_core_gap()
    row.mark_ticketed(merged_url)
    return True


__all__ = [
    "GapSpec",
    "check_gap_checkbox",
    "gap_present",
    "reconcile_merged_gaps",
    "render_checkbox_line",
    "upsert_gap_checkbox",
]
