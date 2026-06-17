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

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import ConsolidatedMemory
from teatree.core.models.implemented_issue_marker import NEEDS_TRIAGE_LABEL
from teatree.core.review_findings import find_bare_references, neutralize_bare_references
from teatree.hooks import banned_terms_scanner
from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)

#: The dedup marker the filer embeds (and searches for) so a re-run never refiles a
#: gap that already has an open tracking issue — mirrors the review-findings
#: fingerprint marker, keyed on the row's stable ``cluster_key``.
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


def triage_disposition(row: ConsolidatedMemory) -> MemoryDisposition:
    """Classify a consolidated rule as user-specific or a core-generic teatree gap.

    Reads the ``durable_destination`` hint the distiller already computed: a home
    under teatree's own skills/source/scripts/BLUEPRINT is core-generic doctrine to
    fix in code; any other home — or no home — is user-specific and stays a memory.
    Conservative on the empty case: an unclassifiable row is kept as memory, never
    auto-ticketed.
    """
    destination = row.durable_destination.strip().lower()
    if destination and destination.startswith(_CORE_DESTINATION_PREFIXES):
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
    repo: str,
    classifier: MemoryClassifier | None = None,
    dry_run: bool = False,
) -> list[TicketOutcome]:
    """Triage every untriaged row; file a deduped teatree ticket for each core gap.

    Each untriaged row is classified (via the injected *classifier*, default the
    ``durable_destination``-hint one). A user-specific row advances to
    ``USER_SPECIFIC_KEEP`` and files nothing. A core-gap row advances to
    ``CORE_GAP_NEEDS_TICKET``; then (unless *dry_run*) a deduped ``needs-triage``
    backlog issue is filed against *repo* and the row advances to ``TICKETED`` with
    the issue URL recorded. A body that would leak a banned term / bare reference is
    withheld — never filed. Returns one outcome per core-gap row (user-specific rows
    file nothing and yield no outcome).
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
        outcomes.append(_file_one_gap(host, row, repo=repo))
    return outcomes


def _file_one_gap(host: CodeHostBackend, row: ConsolidatedMemory, *, repo: str) -> TicketOutcome:
    """File (or reuse) one core-gap backlog ticket and advance the row to TICKETED.

    Dedup-first: an open issue already carrying this row's gap marker is reused. A
    rendered body that would leak a banned term or a bare forge reference is withheld
    (no issue filed). Otherwise a ``needs-triage`` issue is filed and the row records
    its URL.
    """
    existing = _find_existing_gap_issue(host, repo=repo, cluster_key=row.cluster_key)
    if existing:
        row.mark_ticketed(existing)
        return TicketOutcome(cluster_key=row.cluster_key, filed=False, ticket_url=existing, reason="reused open issue")

    title = _ticket_title(row)
    body = _ticket_body(row)
    rendered = f"{title}\n{body}"

    banned = banned_terms_scanner.scan_text(rendered)
    if banned is not None:
        return TicketOutcome(
            cluster_key=row.cluster_key, filed=False, withheld=True, reason=f"contains banned term '{banned}'"
        )
    leaked = find_bare_references(rendered)
    if leaked:
        return TicketOutcome(
            cluster_key=row.cluster_key,
            filed=False,
            withheld=True,
            reason=f"contains bare reference(s): {', '.join(leaked)}",
        )

    raw = host.create_issue(repo=repo, title=title, body=body, labels=["dream-memory-gap", NEEDS_TRIAGE_LABEL])
    url = _issue_url(raw)
    row.mark_ticketed(url)
    return TicketOutcome(cluster_key=row.cluster_key, filed=True, ticket_url=url, reason="filed new issue")


def _find_existing_gap_issue(host: CodeHostBackend, *, repo: str, cluster_key: str) -> str:
    """Return the URL of an open gap issue already carrying this row's marker, or ``""``."""
    marker = f"{_GAP_MARKER} {cluster_key}"
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


def _ticket_body(row: ConsolidatedMemory) -> str:
    rule = neutralize_bare_references(row.rule.strip())
    citation = neutralize_bare_references(row.verified_citation.strip())
    destination = row.durable_destination.strip() or "(unspecified)"
    return (
        "A consolidated memory describes a generic teatree workflow gap. Per "
        "[#2426](https://github.com/souliane/teatree/issues/2426), a generic memory is a "
        "confession that teatree core has a bug — fix it in code (a gate, a hook, a CLI "
        "change) and the memory is retired once the fix lands.\n\n"
        f"**Rule:** {rule}\n\n"
        f"**Cited mistake this would have prevented:** {citation}\n\n"
        f"**Suggested home for the fix:** `{destination}`\n\n"
        f"<!-- {_GAP_MARKER} {row.cluster_key} -->\n"
    )


def retire_resolved_memories(host: CodeHostBackend) -> list[ConsolidatedMemory]:
    """Retire each TICKETED memory whose linked teatree ticket is now closed.

    For every row awaiting ticket-close, the linked issue's state is read from
    *host*; a closed issue retires the row (the prose is archived, the gap it
    confessed is fixed in code). A BINDING row is never retired (binding feedback is
    load-bearing user doctrine). An unreadable issue state keeps the memory — a forge
    hiccup must never retire a memory whose fix may not have landed. Returns the rows
    retired this pass.
    """
    retired: list[ConsolidatedMemory] = []
    for row in ConsolidatedMemory.objects.awaiting_ticket_close():
        if row.is_binding:
            continue
        if not _issue_is_closed(host, row.ticket_url):
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
    "file_core_gap_tickets",
    "retire_resolved_memories",
    "triage_disposition",
]
