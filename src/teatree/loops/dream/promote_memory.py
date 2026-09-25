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
defaulting to the ``durable_destination`` classifier grounded against the core
checkout — :mod:`teatree.loops.dream.destination`), files a deduped teatree
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
from pathlib import Path
from typing import TYPE_CHECKING

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import ConsolidatedMemory
from teatree.core.models.implemented_issue_marker import NEEDS_TRIAGE_LABEL
from teatree.core.review.review_findings import find_bare_references, neutralize_bare_references
from teatree.core.send_proxy import OutboundBlockedError, route_forge_write
from teatree.hooks import banned_terms_scanner
from teatree.loops.dream.destination import classify_destination
from teatree.types import RawAPIDict

if TYPE_CHECKING:
    from collections.abc import Sequence

    from teatree.loops.dream.batch_promote import PromotionBatch
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


class MemoryDisposition(Enum):
    """The two kinds a consolidated rule's lesson splits into during Pass-2 triage."""

    USER_SPECIFIC = "user_specific"
    CORE_GAP = "core_gap"


#: The injected classify seam: a consolidated row → its kind. The default reads the
#: ``durable_destination`` hint Pass 1 already computes; a caller can inject an
#: LLM-backed classifier without changing the file/retire machinery.
MemoryClassifier = Callable[[ConsolidatedMemory], MemoryDisposition]


def triage_disposition(row: ConsolidatedMemory) -> MemoryDisposition:
    """Classify a consolidated rule as user-specific or a core-generic teatree gap.

    Reads the ``durable_destination`` hint the distiller already computed via the
    shared :func:`~teatree.loops.dream.destination.classify_destination` grounding: a
    home the core checkout has a real place for, OR a curated memory file
    (``memory/<slug>.md`` — ``verdict.groundable``), is core-generic doctrine to fix;
    any other home — or no home — is user-specific and stays a memory. A memory-shaped
    destination promotes rather than being kept as memory forever, because that WAS
    the pipeline's whole output: 49 of 49 gaps measured withheld this exact shape
    (#4776). Conservative on the empty case: an unclassifiable row is kept as memory,
    never auto-ticketed. A destination that LOOKS like core but grounds nowhere in the
    tree takes that same conservative path, loudly — the alternative, leaving it
    untriaged, re-warns every pass forever and never drains.
    """
    verdict = classify_destination(row.durable_destination)
    if verdict.groundable:
        return MemoryDisposition.CORE_GAP
    if verdict.reason and row.durable_destination.strip():
        logger.warning(
            "dream: keeping cluster %s as memory — its destination %r is ungrounded: %s (rule=%r).",
            row.cluster_key,
            row.durable_destination,
            verdict.reason,
            row.rule[:120],
        )
    return MemoryDisposition.USER_SPECIFIC


@dataclass(frozen=True, slots=True)
class TicketOutcome:
    """The result of triaging (and possibly ticketing) one consolidated row.

    ``filed`` is True only when a NEW issue was created; ``ticket_url`` is the
    linked issue (newly filed OR a reused open dedup match). ``withheld`` is True
    when the gap was deliberately NOT promoted — the rendered body would leak a
    banned term / bare reference, or its destination grounds nowhere in the core tree.
    """

    cluster_key: str
    filed: bool
    ticket_url: str = ""
    withheld: bool = False
    reason: str = ""


def file_core_gap_tickets(
    *,
    umbrella_url: str = UMBRELLA_ISSUE_URL,
    classifier: MemoryClassifier | None = None,
    dry_run: bool = False,
    batch: "PromotionBatch",
) -> list[TicketOutcome]:
    """Triage every untriaged row; queue each core gap into this pass's batch (#2663, #4776).

    Each untriaged row is classified (via the injected *classifier*, default the
    ``durable_destination``-hint one). A user-specific row advances to
    ``USER_SPECIFIC_KEEP`` and does nothing further. A core-gap row advances to
    ``CORE_GAP_NEEDS_TICKET`` and is QUEUED into *batch* in the SAME pass (the checkbox
    + coding task are minted once, for the whole pass, after every promoting phase has
    run). The gap no longer files a fresh ``needs-triage`` issue that the scanner
    skips. A rendered title that would leak a banned term / bare reference is withheld
    — never queued. A queued row is stamped TICKETED once the pass mints its batch
    ticket, a row already covered by a ticket is stamped here, and a withheld,
    ungrounded or dry-run row is never stamped.

    Under *dry_run* NOTHING is written — no disposition advance, no queueing — so a
    preview never STRANDS a detected gap: the disposition write used to land BEFORE the
    dry-run guard, moving the row out of ``untriaged()`` while its promotion was
    skipped, so the gap sat in ``CORE_GAP_NEEDS_TICKET`` with no drain and was silently
    detected but never fixed.

    Before the untriaged queue, any core-gap row a prior pass classified but never
    promoted (:meth:`ConsolidatedMemory.objects.needs_ticket` — no ticket recorded) is
    drained first, so such a stranded gap is picked up and queued rather than left
    forever. Returns one outcome per queued core-gap row (user-specific rows and a
    dry-run yield no outcome).
    """
    classify = classifier or triage_disposition
    outcomes: list[TicketOutcome] = []
    if dry_run:
        # A preview must not mutate: classify nothing, promote nothing. The untriaged
        # rows stay untriaged so the next real pass drains them faithfully.
        return outcomes
    # Drain gaps a prior pass classified but never promoted (e.g. stranded by an older
    # dry-run) FIRST, before this pass classifies any new core gap — reading the queue
    # up front means a row classified below is never re-drained in the same pass.
    outcomes.extend(
        _promote_one_gap(stranded, umbrella_url=umbrella_url, batch=batch)
        for stranded in ConsolidatedMemory.objects.needs_ticket()
    )
    for row in ConsolidatedMemory.objects.untriaged():
        if classify(row) is MemoryDisposition.USER_SPECIFIC:
            row.classify_user_specific()
            continue
        row.classify_core_gap()
        outcomes.append(_promote_one_gap(row, umbrella_url=umbrella_url, batch=batch))
    return outcomes


def _promote_one_gap(row: ConsolidatedMemory, *, umbrella_url: str, batch: "PromotionBatch") -> TicketOutcome:
    """Queue one core-gap row into this pass's promotion batch (#2663, #4776).

    Reuses :meth:`~teatree.loops.dream.batch_promote.PromotionBatch.consider`: the gap
    is deduped (by ``cluster_key``) against every in-flight/delivered batch and queued
    for the SINGLE ticket the pass mints once every phase has run. The banned-term /
    bare-reference withholding is enforced inside ``consider`` against the rendered
    title.

    The destination is re-grounded HERE and not only at triage, because the
    ``needs_ticket()`` drain promotes rows a PRIOR pass classified without re-running
    the classifier — so this is the one chokepoint every promoted gap passes through.
    ``groundable`` (core tree OR a memory file) is the test, not ``in_core_tree``
    alone — a memory-destined gap promotes rather than being withheld (#4776).

    A gap already riding an unreconciled ticket is stamped with it here, so it leaves
    the queue; a newly queued gap is stamped when :func:`promote_batch` mints its ticket.
    """
    from teatree.loops.dream.batch_promote import covering_ticket, is_reconciled  # noqa: PLC0415 — tick-time import
    from teatree.loops.dream.umbrella_ledger import GapSpec  # noqa: PLC0415 — deferred: loaded at tick time, not import

    verdict = classify_destination(row.durable_destination)
    if not verdict.groundable:
        logger.warning(
            "dream: refusing to promote cluster %s — its destination %r is ungrounded: %s.",
            row.cluster_key,
            row.durable_destination,
            verdict.reason,
        )
        return TicketOutcome(
            cluster_key=row.cluster_key,
            filed=False,
            withheld=True,
            reason=f"ungrounded destination: {verdict.reason}",
        )

    outcome = batch.consider(
        gap=GapSpec(gap_key=row.cluster_key, title=_ticket_title(row), cluster_key=row.cluster_key)
    )
    ticket = covering_ticket(row.cluster_key) if outcome.already_covered and not outcome.withheld else None
    if ticket is not None and not is_reconciled(ticket):
        row.mark_ticketed(ticket.issue_url)
    return TicketOutcome(
        cluster_key=row.cluster_key,
        filed=outcome.queued or outcome.already_covered,
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
    *host*); a resolved ticket first has its source memory FILE(s) deleted
    (:func:`delete_source_memory_files` — "memory tends to zero": a promoted gap whose
    fix merged has nothing left to remember once its source is gone) and only THEN
    retires the DB row (the prose is archived, the gap it confessed is fixed in code).
    A row whose source file could not be confirmed deleted stays TICKETED and is
    retried next pass — never retired with a dangling file. A BINDING row is
    PERMANENTLY exempt from both file deletion and DB retirement (binding feedback is
    hand-flagged, load-bearing user doctrine — the one class this pipeline does not
    unilaterally judge "done"). An unresolved/unreadable ticket keeps the memory — a
    forge hiccup must never retire a memory whose fix may not have landed.

    The *is_resolved* seam lets the umbrella reconcile path
    (:func:`teatree.loops.dream.umbrella_ledger.reconcile_merged_gaps`,
    :func:`teatree.loops.dream.batch_promote.reconcile_batches`) retire off the gap-fix
    Ticket's authoritative MERGED state instead of a fragile forge re-read of a PR URL
    (a ``/pull/<n>`` URL the issue endpoint does not serve). Returns the rows retired
    this pass.
    """
    resolved = is_resolved or (lambda url: _issue_is_closed(host, url))
    retired: list[ConsolidatedMemory] = []
    for row in ConsolidatedMemory.objects.awaiting_ticket_close():
        if row.is_binding:
            continue
        if not resolved(row.ticket_url):
            continue
        if not delete_source_memory_files(row):
            logger.warning(
                "dream: deferring retirement of cluster %s — a source memory file "
                "could not be confirmed deleted, retrying next pass",
                row.cluster_key,
            )
            continue
        row.retire(archive_path=row.ticket_url)
        retired.append(row)
    return retired


def delete_source_memory_files(row: ConsolidatedMemory) -> bool:
    """Delete *row*'s source files that live under a discovered memory dir, verified.

    "Memory must tend to zero": a promoted gap's source memory file is DELETED on
    retirement, not merely archived-by-budget-decay, so the personal-memory corpus
    actually shrinks as gaps get fixed in code. Only ``source_files`` entries that
    resolve inside a :func:`~teatree.memory_audit.discover_memory_dirs` root with a
    ``.md`` suffix are candidates — a row's sources may also carry non-memory
    references (e.g. a transcript path), which are never touched. Each delete is
    VERIFIED by re-reading the path afterward, not trusted from the ``unlink()`` call
    alone. Returns True iff every candidate is confirmed gone — vacuously True when
    there are no memory-dir candidates, so a core-code-only gap's retirement is never
    blocked by a check that has nothing to confirm.
    """
    from teatree.memory_audit import discover_memory_dirs  # noqa: PLC0415 — deferred: stdlib-only, loaded at tick time

    roots = discover_memory_dirs()
    if not roots:
        return True
    all_confirmed = True
    for raw_path in row.source_files:
        path = Path(str(raw_path))
        if path.suffix.lower() != ".md" or not any(_resolves_within(path, root) for root in roots):
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("dream: could not delete source memory file %s (cluster %s): %s", path, row.cluster_key, exc)
            all_confirmed = False
            continue
        if path.exists():
            logger.warning("dream: source memory file %s (cluster %s) still exists after delete", path, row.cluster_key)
            all_confirmed = False
    return all_confirmed


def _resolves_within(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


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
    "delete_source_memory_files",
    "file_binding_reconciliation_tickets",
    "file_core_gap_tickets",
    "retire_resolved_memories",
    "triage_disposition",
]
