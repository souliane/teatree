"""Dreaming Pass 2 — promote core-generic memories into teatree fixes (#2426).

Pass 1 (the consolidation engine) reads recent transcripts + curated memories and
writes consolidated rules into the :class:`~teatree.core.models.ConsolidatedMemory`
ledger. On its own that is "retro with a database" — nicer memories, but it does
not reduce teatree's dependence on memory.

Pass 2 drains the ledger. Every consolidated rule splits in two:

*   **user-specific** (personal tone, local paths, per-user workflow) — legitimately
    stays as memory; teatree cannot encode it;
*   **core-generic** ("a gate must fail loud, never skip-as-pass", "run tree-wide
    health before push") — a gap in teatree's own workflow, a confession that core
    has a bug. It must be fixed in code, and the memory retired once that fix lands.

So Pass 2 triages each untriaged row (:func:`triage_disposition`, an injected seam
defaulting to the ``durable_destination``-hint classifier), files a deduped teatree
backlog ticket for the core-generic ones (:func:`file_core_gap_tickets` — the same
durable, reversible move that converted harness TODOs into ``backlog`` issues),
and retires the prose once the linked ticket closes (:func:`retire_resolved_memories`).
A BINDING row is never retired — binding feedback is load-bearing user doctrine.

The classify step is an INJECTED seam and the forge writes go through a passed-in
:class:`~teatree.core.backend_protocols.CodeHostBackend`, so the whole pass is
testable without an LLM and without a live forge. Per the design issue, Pass 2
auto-files the *ticket* (durable, reversible) but never auto-implements — the fix
is left for a human / the loop to pick up, and the filed issue self-applies
``needs-triage`` so the loop's claim gate withholds it until the maintainer clears it.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import ConsolidatedMemory
from teatree.core.models.implemented_issue_marker import NEEDS_TRIAGE_LABEL
from teatree.core.review.review_findings import find_bare_references, neutralize_bare_references
from teatree.core.send_proxy import OutboundBlockedError, route_forge_write
from teatree.hooks import banned_terms_scanner
from teatree.types import RawAPIDict

if TYPE_CHECKING:
    from collections.abc import Sequence

    from teatree.loops.dream.merge import BindingConflict

logger = logging.getLogger(__name__)

#: The standing umbrella issue that tracks every grounded dream gap as a reusable
#: checkbox ledger — reused daily, never closed (#2663). A core gap rides this
#: umbrella + a scheduled coding task instead of a fresh ``needs-triage`` issue.
UMBRELLA_ISSUE_URL = "https://github.com/souliane/teatree/issues/2663"

#: The dedup marker the binding-reconciliation filer embeds (and searches for) so a
#: re-run never refiles a conflict that already has an open tracking issue — mirrors
#: the review-findings fingerprint marker.
_GAP_MARKER = "dream-memory-gap"

#: A consolidated rule whose durable home points at teatree's own code/skills is a
#: core-generic workflow gap (fix in code); any other home (a personal topic file,
#: or no home) is user-specific and stays a memory. These are the prefixes the
#: default classifier reads as "this belongs in teatree core".
_CORE_DESTINATION_PREFIXES = ("skills/", "src/teatree", "teatree/", "scripts/", "blueprint", "agents/")


class MemoryDisposition(Enum):
    """The two kinds a consolidated rule's lesson splits into during Pass-2 triage."""

    USER_SPECIFIC = "user_specific"
    CORE_GAP = "core_gap"


#: The injected classify seam: a consolidated row → its kind. The default reads the
#: ``durable_destination`` hint Pass 1 already computes; a caller can inject an
#: LLM-backed classifier without changing the file/retire machinery.
MemoryClassifier = Callable[[ConsolidatedMemory], MemoryDisposition]


def points_at_core_fix(destination: str) -> bool:
    """Whether *destination* names a teatree-core fix path (skills / src / scripts / BLUEPRINT / …).

    The single classifier behind Pass-2 triage (:func:`triage_disposition`) and the
    compliance recurrence redirect
    (:func:`teatree.loops.dream.compliance._is_memory_only`): a home under teatree's
    own code/skills is a core-generic gap to fix in code; any other home — or no
    home — is user-specific and stays a memory.
    """
    home = destination.strip().lower()
    return bool(home) and home.startswith(_CORE_DESTINATION_PREFIXES)


def triage_disposition(row: ConsolidatedMemory) -> MemoryDisposition:
    """Classify a consolidated rule as user-specific or a core-generic teatree gap.

    Reads the ``durable_destination`` hint the distiller already computed via the
    shared :func:`points_at_core_fix` classifier: a home under teatree's own
    skills/source/scripts/BLUEPRINT is core-generic doctrine to fix in code; any
    other home — or no home — is user-specific and stays a memory. Conservative on
    the empty case: an unclassifiable row is kept as memory, never auto-ticketed.
    """
    if points_at_core_fix(row.durable_destination):
        return MemoryDisposition.CORE_GAP
    return MemoryDisposition.USER_SPECIFIC


@dataclass(frozen=True, slots=True)
class TicketOutcome:
    """The result of triaging (and possibly ticketing) one consolidated row.

    ``filed`` is True only when a NEW issue was created; ``ticket_url`` is the
    linked issue (newly filed OR a reused open dedup match). ``withheld`` is True
    when the rendered body would leak a banned term / bare reference and the issue
    was deliberately NOT filed.
    """

    cluster_key: str
    filed: bool
    ticket_url: str = ""
    withheld: bool = False
    reason: str = ""


def file_core_gap_tickets(
    host: CodeHostBackend,
    *,
    umbrella_url: str = UMBRELLA_ISSUE_URL,
    classifier: MemoryClassifier | None = None,
    dry_run: bool = False,
) -> list[TicketOutcome]:
    """Triage every untriaged row; drive each core gap to a fix-and-merge (#2663).

    Each untriaged row is classified (via the injected *classifier*, default the
    ``durable_destination``-hint one). A user-specific row advances to
    ``USER_SPECIFIC_KEEP`` and does nothing further. A core-gap row advances to
    ``CORE_GAP_NEEDS_TICKET``; then (unless *dry_run*) it is PROMOTED to a fix —
    a checkbox is upserted under the standing umbrella issue (*umbrella_url*, deduped
    by the row's ``cluster_key``) and a coding task is scheduled for the fix. The gap
    no longer files a fresh ``needs-triage`` issue that the scanner skips. A rendered
    title that would leak a banned term / bare reference is withheld — never written.
    Returns one outcome per core-gap row (user-specific rows yield no outcome).
    """
    classify = classifier or triage_disposition
    outcomes: list[TicketOutcome] = []
    for row in ConsolidatedMemory.objects.untriaged():
        if classify(row) is MemoryDisposition.USER_SPECIFIC:
            row.classify_user_specific()
            continue
        row.classify_core_gap()
        if dry_run:
            continue
        outcomes.append(_promote_one_gap(host, row, umbrella_url=umbrella_url))
    return outcomes


def _promote_one_gap(host: CodeHostBackend, row: ConsolidatedMemory, *, umbrella_url: str) -> TicketOutcome:
    """Drive one core-gap row to a fix-and-merge via the umbrella ledger (#2663).

    Reuses :func:`teatree.loops.dream.umbrella_ledger.promote_gap`: a checkbox is
    upserted under the umbrella (deduped by ``cluster_key``) and a coding task is
    scheduled for the fix (deduped by the same key). The banned-term / bare-reference
    withholding is enforced inside ``promote_gap`` against the rendered title.
    """
    from teatree.loops.dream import umbrella_ledger  # noqa: PLC0415 — deferred: loaded at tick time, not import

    outcome = umbrella_ledger.promote_gap(
        host,
        umbrella_url=umbrella_url,
        gap=umbrella_ledger.GapSpec(gap_key=row.cluster_key, title=_ticket_title(row), cluster_key=row.cluster_key),
    )
    return TicketOutcome(
        cluster_key=row.cluster_key,
        filed=outcome.scheduled or outcome.checkbox_added,
        ticket_url=umbrella_url,
        withheld=outcome.withheld,
        reason=outcome.reason,
    )


def _withholding_reason(rendered: str) -> str:
    """The reason a rendered body must be withheld (banned term / bare ref), or ``""``."""
    banned = banned_terms_scanner.scan_text(rendered)
    if banned is not None:
        return f"contains banned term '{banned}'"
    leaked = find_bare_references(rendered)
    if leaked:
        return f"contains bare reference(s): {', '.join(leaked)}"
    return ""


#: The dedup marker for a binding-reconciliation ticket — keyed on the conflicting
#: PAIR's sorted file stems so a re-run never refiles a conflict already tracked.
_RECONCILE_MARKER = "dream-binding-reconcile"


def _conflict_key(conflict: "BindingConflict") -> str:
    return "+".join(sorted((conflict.survivor_name, conflict.absorbed_name)))


def file_binding_reconciliation_tickets(
    host: CodeHostBackend, *, repo: str, conflicts: "Sequence[BindingConflict]", dry_run: bool = False
) -> list[TicketOutcome]:
    """File a deduped reconciliation ticket per conflicting-BINDING memory pair (#2723).

    Two BINDING near-duplicates are never auto-merged (Decision-3); the merge phase
    cross-links them and hands the pair here. A deduped ``dream-memory-gap`` issue is
    filed against *repo* so a human reconciles the doctrine. Dedup-first on the pair's
    sorted stems, banned-term / bare-reference withholding reused verbatim from the
    core-gap filer. Under *dry_run* nothing is filed. Returns one outcome per pair.
    """
    outcomes: list[TicketOutcome] = []
    for conflict in conflicts:
        if dry_run:
            continue
        outcomes.append(_file_one_reconciliation(host, conflict, repo=repo))
    return outcomes


def _file_one_reconciliation(host: CodeHostBackend, conflict: "BindingConflict", *, repo: str) -> TicketOutcome:
    key = _conflict_key(conflict)
    marker = f"{_RECONCILE_MARKER} {key}"
    existing = _find_existing_marker_issue(host, repo=repo, marker=marker)
    if existing:
        return TicketOutcome(cluster_key=key, filed=False, ticket_url=existing, reason="reused open issue")

    title = f"Conflicting BINDING memories need reconciliation: {neutralize_bare_references(key)}"
    body = (
        "Two BINDING memory files are near-duplicates but cannot be auto-merged — "
        "binding doctrine that disagrees must be reconciled by a human, not silently "
        "collapsed. The dream merge phase cross-linked them; please decide which rule "
        "is canonical and retire or rewrite the other.\n\n"
        f"**Files:** `{conflict.survivor_name}.md`, `{conflict.absorbed_name}.md`\n\n"
        f"<!-- {marker} -->\n"
    )
    reason = _withholding_reason(f"{title}\n{body}")
    if reason:
        return TicketOutcome(cluster_key=key, filed=False, withheld=True, reason=reason)

    # Route through the shared forge-write seam (public-repo leak gate + #117
    # send-proxy audit), the same path the MCP tools use — so this dream-loop
    # write is no longer unscrubbed. A leak/blocked verdict withholds the issue.
    try:
        title = route_forge_write(forge="", repo=repo, text=title, action="dream_reconcile", target=repo)
        body = route_forge_write(forge="", repo=repo, text=body, action="dream_reconcile", target=repo)
    except OutboundBlockedError as exc:
        return TicketOutcome(cluster_key=key, filed=False, withheld=True, reason=str(exc))

    raw = host.create_issue(repo=repo, title=title, body=body, labels=[_GAP_MARKER, NEEDS_TRIAGE_LABEL])
    return TicketOutcome(cluster_key=key, filed=True, ticket_url=_issue_url(raw), reason="filed new issue")


def _find_existing_marker_issue(host: CodeHostBackend, *, repo: str, marker: str) -> str:
    """Return the URL of an open issue already carrying *marker*, or ``""``."""
    try:
        matches = host.search_open_issues(repo=repo, query=marker)
    except Exception:  # noqa: BLE001 — a search hiccup must not block filing; refile-once self-corrects.
        return ""
    for raw in matches:
        body = str(raw.get("body") or raw.get("description") or "")
        if marker in body:
            return _issue_url(raw)
    return ""


def _ticket_title(row: ConsolidatedMemory) -> str:
    snippet = neutralize_bare_references(row.rule.strip().split(". ")[0][:60].rstrip())
    return f"Workflow gap (dreaming Pass 2): {snippet}"


def retire_resolved_memories(
    host: CodeHostBackend, *, is_resolved: "Callable[[str], bool] | None" = None
) -> list[ConsolidatedMemory]:
    """Retire each TICKETED memory whose linked teatree ticket is now resolved.

    For every row awaiting ticket-close, the linked ticket's resolved state is read
    via *is_resolved* (default: the linked issue's closed/merged state read from
    *host*); a resolved ticket retires the row (the prose is archived, the gap it
    confessed is fixed in code). A BINDING row is never retired (binding feedback is
    load-bearing user doctrine). An unresolved/unreadable ticket keeps the memory — a
    forge hiccup must never retire a memory whose fix may not have landed.

    The *is_resolved* seam lets the umbrella reconcile path
    (:func:`teatree.loops.dream.umbrella_ledger.reconcile_merged_gaps`) retire off the
    gap-fix Ticket's authoritative MERGED state instead of a fragile forge re-read of
    a PR URL (a ``/pull/<n>`` URL the issue endpoint does not serve). Returns the rows
    retired this pass.
    """
    resolved = is_resolved or (lambda url: _issue_is_closed(host, url))
    retired: list[ConsolidatedMemory] = []
    for row in ConsolidatedMemory.objects.awaiting_ticket_close():
        if row.is_binding:
            continue
        if not resolved(row.ticket_url):
            continue
        row.retire(archive_path=row.ticket_url)
        retired.append(row)
    return retired


def _issue_is_closed(host: CodeHostBackend, issue_url: str) -> bool:
    """Whether the linked issue is closed; an unreadable state fails to KEEP (not retire)."""
    try:
        raw = host.get_issue(issue_url)
    except Exception:  # noqa: BLE001 — a forge error must not retire an un-fixed memory.
        return False
    state = str(raw.get("state") or "").strip().lower()
    return state in {"closed", "merged"}


def _issue_url(raw: RawAPIDict) -> str:
    for key in ("html_url", "web_url", "url"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


__all__ = [
    "MemoryClassifier",
    "MemoryDisposition",
    "TicketOutcome",
    "file_binding_reconciliation_tickets",
    "file_core_gap_tickets",
    "points_at_core_fix",
    "retire_resolved_memories",
    "triage_disposition",
]
