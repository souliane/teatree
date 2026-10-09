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
gaps into one :class:`~teatree.loops.dream.batch_promote.PromotionBatch`, which
queues them for the backlog sweep and mints no ticket. This module stays the
durable umbrella checkbox ledger.

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

from teatree.backends.loader import get_code_host_for_url
from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import ConsolidatedMemory
from teatree.core.models.ticket import Ticket
from teatree.core.overlay_loader import get_all_overlays, infer_overlay_for_url
from teatree.core.review.review_findings import find_bare_references, neutralize_bare_references
from teatree.core.send_proxy import OutboundBlockedError, forge_from_url, route_forge_write
from teatree.hooks import banned_terms_scanner
from teatree.utils.url_slug import project_slug_from_ref

logger = logging.getLogger(__name__)

#: The stable marker embedded (invisibly) in each umbrella checkbox line, keyed on
#: the gap key, so a re-run upserts in place rather than appending a duplicate.
_GAP_MARKER_PREFIX = "dream-gap"
_BATCH_MARKER_PREFIX = "dream-batch"

_UMBRELLA_KEY = "dream_umbrella_url"
_RECONCILED_KEY = "dream_gap_reconciled_at"


@dataclass(frozen=True, slots=True)
class GapSpec:
    """One grounded gap's identity for promotion to a fix-and-merge.

    ``gap_key`` is the stable umbrella-checkbox / scheduling dedup key; ``title`` is
    the rendered checkbox label (scanned for banned terms / bare refs before any
    write); ``cluster_key`` links the gap-fix Ticket back to the ``ConsolidatedMemory``
    row to retire on merge (equal to ``gap_key`` for a core gap; the
    ``compliance-recurrence-<rule_identity>`` key for a recurrence).
    """

    gap_key: str
    title: str
    cluster_key: str
    detail: str = ""


def is_promotion_anchor(url: str) -> bool:
    """Whether a TICKETED row's url is a promotion-time placeholder rather than a merged PR."""
    return f"#{_BATCH_MARKER_PREFIX}=" in url


def _marker(gap_key: str) -> str:
    return f"<!-- {_GAP_MARKER_PREFIX} {gap_key} -->"


def gap_title(prefix: str, rule: str) -> str:
    """A gap's checkbox label: *rule*'s first sentence under *prefix*, truncated then scanned.

    Truncation happens BEFORE the bare-reference scan, so what is scanned is exactly
    what is written — cutting a neutralised title could re-expose what the scan replaced.
    """
    return f"{prefix}: {neutralize_bare_references(rule.strip().split('. ')[0][:60].rstrip())}"


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
            repo=project_slug_from_ref(umbrella_url),
            text=body,
            action="dream_umbrella_update",
            target=umbrella_url,
        )
    except OutboundBlockedError:
        return False
    host.update_issue(issue_url=umbrella_url, body=clean)
    return True


def code_host_for(issue_url: str) -> CodeHostBackend | None:
    """The forge client for *issue_url*, on the owning overlay's credentials first."""
    owner = infer_overlay_for_url(issue_url)
    overlays = sorted(get_all_overlays().items(), key=lambda item: item[0] != owner)
    return next(
        (host for _name, overlay in overlays if (host := get_code_host_for_url(overlay, issue_url)) is not None),
        None,
    )


def _read_body(host: CodeHostBackend, umbrella_url: str) -> str | None:
    """Re-read the umbrella body; ``None`` on an unreadable or foreign umbrella (never raises).

    The read goes through the #162 Rule 5 guard rather than a bare ``get_issue``, so
    the body every upsert is computed from is the body we were AUTHORISED on — one
    fetch for both facts. An umbrella the owner / factory bot did not file is not
    ours to rewrite, and reads as unreadable here: the pass leaves it untouched,
    exactly as it does for a forge hiccup.
    """
    from teatree.core.self_forge_identities import (  # noqa: PLC0415 — deferred: ORM-adjacent import
        ExternalIssueRefusedError,
        require_self_authored_issue,
    )

    try:
        raw = require_self_authored_issue(host=host, issue_url=umbrella_url)
    except ExternalIssueRefusedError:
        logger.warning("umbrella %s was filed by someone else — leaving it untouched", umbrella_url)
        return None
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


def _merge_evidence_url(ticket: Ticket) -> str:
    """The merged PR backing this MERGED gap-fix ticket, else the ticket's own url."""
    from teatree.core.models.pull_request import PullRequest  # noqa: PLC0415 — deferred: ORM/app-registry

    pr = PullRequest.objects.filter(ticket=ticket, state=PullRequest.State.MERGED).first()
    return pr.url if pr is not None else ticket.issue_url


def _stamp_ticket_reconciled(ticket: Ticket) -> None:
    """Mark a gap-fix Ticket reconciled so later passes skip it (F6.9).

    Records :data:`_RECONCILED_KEY` in the ticket's ``extra``; the next
    the batch scan excludes it, so a merged gap is reconciled once,
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


def _stamp_memory_promoted(cluster_key: str, *, anchor_url: str) -> bool:
    """Stamp a still-queued gap's memory TICKETED with the ticket that now carries its fix."""
    row = ConsolidatedMemory.objects.needs_ticket().filter(cluster_key=cluster_key).first()
    if row is None:
        return False
    row.mark_ticketed(anchor_url)
    return True


def _stamp_memory_merged(cluster_key: str, *, merged_url: str) -> bool:
    """Point the gap's memory at its merged fix so the existing retire path fires.

    The memory is advanced to TICKETED with the merged PR as its ``ticket_url`` (the
    reconcile then drives ``retire_resolved_memories`` off the authoritative MERGED
    signal). A row stamped TICKETED at promotion (:func:`is_promotion_anchor`) is
    re-stamped here; one already TICKETED on a real PR, retired, or with no merged URL
    is left untouched. Returns True iff this stamped a row TICKETED with *merged_url*.
    """
    if not cluster_key or not merged_url:
        return False
    row = ConsolidatedMemory.objects.filter(cluster_key=cluster_key).first()
    if row is None:
        return False
    promoted = row.disposition == ConsolidatedMemory.Disposition.TICKETED and is_promotion_anchor(row.ticket_url)
    if not promoted and row.disposition not in {
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
    "code_host_for",
    "gap_present",
    "gap_title",
    "is_promotion_anchor",
    "render_checkbox_line",
    "upsert_gap_checkbox",
]
