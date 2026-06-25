"""Dream automatable-ask promotion — the "improve-with-new-stuff" half (#2663).

The structural sibling of :mod:`teatree.loops.dream.compliance`. Compliance is the
"fix-issues" half — a rule was PRESENT and the agent violated it. This phase is the
"improve-with-new-stuff" half — a recurring MANUAL user ask t3 could automate so the
user "gets out of the loop". Both express themselves ENTIRELY within the memory verbs
(consolidate → merge → promote → retire); neither is a bolt-on parallel subsystem.

The pipeline reuses the existing machinery end to end — there is NO new model.

DETECT rides :func:`teatree.loops.dream.transcript_extract.looks_like_user_ask` (the
keyword-blind keeper for a USER directive/request), already wired into the engine's
``high_signal_lines``, so repeated asks reach the distiller and cluster into
:class:`~teatree.core.models.ConsolidatedMemory` ask-clusters (same ``cluster_key``
upsert dedups/merges, exactly like corrections). CLASSIFY sends each ask-cluster to
Bucket A (``EXISTING_GAP`` — an existing loop/skill SHOULD have handled this;
:func:`classify_ask_cluster` names the mechanism from the injected
:data:`AUTOMATION_CATALOG`) or Bucket B (``NEW_WORKFLOW`` — no automation exists;
prescribe a new loop/skill/gate, canonical example a hotfix lane). PROMOTE routes each
GROUNDED automatable-ask gap (the verbatim cited snippet must appear in the transcript
— :func:`teatree.loops.dream.engine.cluster_is_grounded`, mirroring the cluster
grounding) through :func:`teatree.loops.dream.umbrella_ledger.promote_gap`: a deduped
umbrella checkbox under the standing umbrella issue + a scheduled coding fix, carrying
the Bucket-A/B framing in the title. RETIRE reuses Part C's
:func:`~teatree.loops.dream.umbrella_ledger.reconcile_merged_gaps` — when the ask's
automation fix merges, the ask memory is retired off the gap-fix Ticket's MERGED state.

PURE w.r.t. the forge and the LLM: the classifier is an INJECTED seam (default the
deterministic catalog-keyword classifier) and the forge writes go through a passed-in
:class:`~teatree.core.backend_protocols.CodeHostBackend`, so the whole phase is
testable without an LLM and without a live forge.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import ConsolidatedMemory
from teatree.loops.dream.engine import ConsolidationExtract, DistilledCluster, cluster_is_grounded, normalize_ws
from teatree.loops.dream.transcript_extract import _ASK_CUES

#: The dream-automation-gap label carried in a promoted ask gap's brief — the lineage
#: tag distinguishing an automatable-ask gap from a compliance recurrence or core gap.
DREAM_AUTOMATION_LABEL = "dream-automation-gap"


class AskBucket(Enum):
    """The two kinds a recurring automatable user ask classifies into.

    ``EXISTING_GAP`` (Bucket A) — an existing loop/skill SHOULD have handled this ask;
    the named mechanism's trigger/coverage/surfacing is what the fix prescribes.
    ``NEW_WORKFLOW`` (Bucket B) — no automation exists; the fix prescribes a new
    loop/skill/gate (canonical example: a hotfix lane).
    """

    EXISTING_GAP = "existing_gap"
    NEW_WORKFLOW = "new_workflow"


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One loop/skill the classifier can name as the existing mechanism (Bucket A).

    ``name`` is the mechanism handle that lands in a Bucket-A fix brief; ``cues`` are
    the distinctive lowercase tokens a recurring ask must share with it to be read as
    "this mechanism already exists and SHOULD have handled the ask".
    """

    name: str
    cues: tuple[str, ...]


#: The loop + skill catalog injected into the classifier so Bucket-A names a REAL
#: mechanism rather than a vague "some loop". Each entry maps a distinctive cue set to
#: the loop/skill that owns that surface — so an ask whose subject overlaps one is
#: classified EXISTING_GAP against that named mechanism, and an ask overlapping none is
#: NEW_WORKFLOW (e.g. a hotfix lane, which no current loop owns).
AUTOMATION_CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry("followup-loop", ("follow-up", "followup", "nag", "reminder", "stale", "review request")),
    CatalogEntry("review-loop", ("review", "approve", "merge", "mr", "pull request")),
    CatalogEntry("tickets-loop", ("ticket", "backlog", "issue", "assign")),
    CatalogEntry("issue-implementer-loop", ("implement", "fix the issue", "burn down", "claim")),
    CatalogEntry("dispatch-loop", ("dispatch", "sub-agent", "subagent", "fan out", "parallel agent")),
    CatalogEntry("housekeeping-loop", ("worktree", "clean up", "clean-all", "stale branch", "prune")),
    CatalogEntry("inbox-loop", ("slack", "dm", "inbound", "notification", "message")),
    CatalogEntry("audit-loop", ("audit", "outbound", "leak", "privacy", "banned term")),
    CatalogEntry("resource-pressure-loop", ("disk", "memory pressure", "oom", "reclaim", "resource")),
    CatalogEntry("workspace-skill", ("set up", "provision", "worktree", "database", "dev server")),
    CatalogEntry("ship-skill", ("push", "open the pr", "open a pr", "create the pr", "ship", "commit")),
    CatalogEntry("e2e-skill", ("e2e", "browser", "screenshot", "visual qa", "evidence")),
)


@dataclass(frozen=True, slots=True)
class AutomationAskFinding:
    """One classified automatable-ask cluster — the typed phase record.

    ``bucket`` is Bucket A (``EXISTING_GAP``) or Bucket B (``NEW_WORKFLOW``);
    ``mechanism`` names the existing loop/skill for a Bucket-A finding (empty for
    Bucket B); ``rule`` is the consolidated ask rule the title is rendered from.
    """

    cluster_key: str
    bucket: AskBucket
    mechanism: str
    rule: str


@dataclass(frozen=True, slots=True)
class AutomationAskOutcome:
    """The result of promoting one grounded automatable-ask gap to a fix-and-merge.

    ``filed`` is True when a NEW umbrella checkbox was added OR a coding task was
    scheduled; ``ticket_url`` is the umbrella issue URL; ``withheld`` is True when the
    rendered title would leak a banned term / bare reference.
    """

    cluster_key: str
    bucket: AskBucket
    filed: bool
    ticket_url: str = ""
    withheld: bool = False
    reason: str = ""


#: The injected classify seam: an ask cluster → its typed finding. The default reads
#: the :data:`AUTOMATION_CATALOG`; a caller can inject an LLM-backed classifier without
#: changing the detect/promote machinery — mirrors ``promote_memory.MemoryClassifier``.
AskClassifier = Callable[[DistilledCluster], AutomationAskFinding]


def _match_mechanism(rule: str) -> str:
    """The catalog mechanism whose cues the ask rule overlaps best, or ``""`` (Bucket B).

    A cue is matched as a substring (so multi-word cues like ``"review request"`` and
    ``"pull request"`` match) and the entry with the most matched cues wins; ties break
    on catalog order. No matched cue → no existing mechanism → Bucket B.
    """
    lowered = rule.lower()
    best_name = ""
    best_score = 0
    for entry in AUTOMATION_CATALOG:
        score = sum(1 for cue in entry.cues if cue in lowered)
        if score > best_score:
            best_name = entry.name
            best_score = score
    return best_name


def classify_ask_cluster(cluster: DistilledCluster, *, classifier: AskClassifier | None = None) -> AutomationAskFinding:
    """Classify one ask cluster Bucket A (existing mechanism) or Bucket B (new workflow).

    The default classifier matches the consolidated ask rule against the injected
    :data:`AUTOMATION_CATALOG`: a rule overlapping a known loop/skill's cues is
    ``EXISTING_GAP`` naming that mechanism; a rule overlapping none is ``NEW_WORKFLOW``.
    A caller may inject an LLM-backed *classifier* (the seam) without touching the
    detect/promote machinery.
    """
    if classifier is not None:
        return classifier(cluster)
    mechanism = _match_mechanism(cluster.rule)
    bucket = AskBucket.EXISTING_GAP if mechanism else AskBucket.NEW_WORKFLOW
    return AutomationAskFinding(cluster_key=cluster.cluster_key, bucket=bucket, mechanism=mechanism, rule=cluster.rule)


def detect_automatable_asks(
    clusters: Sequence[DistilledCluster],
    extract: ConsolidationExtract,
    *,
    classifier: AskClassifier | None = None,
) -> list[AutomationAskFinding]:
    """Detect + classify every GROUNDED automatable-ask cluster in one pass.

    Reuses the cluster grounding guard
    (:func:`teatree.loops.dream.engine.cluster_is_grounded`): a cluster whose
    ``verified_citation`` does not appear verbatim in a cited extract snippet — or
    whose cited path is absent from the extract — is dropped, exactly like the cluster
    grounding the ledger enforces. Each surviving cluster is classified into a typed
    :class:`AutomationAskFinding`.
    """
    snippet_texts = {str(snippet.path): normalize_ws(snippet.text) for snippet in extract.snippets}
    findings: list[AutomationAskFinding] = []
    for cluster in clusters:
        if not cluster_is_grounded(cluster, snippet_texts):
            continue
        findings.append(classify_ask_cluster(cluster, classifier=classifier))
    return findings


def _ask_title(finding: AutomationAskFinding) -> str:
    """Render the umbrella checkbox title carrying the Bucket-A/B framing + the label."""
    snippet = finding.rule.strip().split(". ")[0][:80].rstrip()
    if finding.bucket is AskBucket.EXISTING_GAP:
        framing = f"existing gap (`{finding.mechanism}`) — fix its trigger/coverage/surfacing"
    else:
        framing = "new workflow — prescribe a new loop/skill/gate (e.g. a hotfix lane)"
    return f"Automatable ask [{DREAM_AUTOMATION_LABEL}]: {snippet} — {framing}"


def promote_automatable_asks(
    clusters: Sequence[DistilledCluster],
    extract: ConsolidationExtract,
    host: CodeHostBackend,
    *,
    umbrella_url: str,
    dry_run: bool = False,
) -> list[AutomationAskOutcome]:
    """Drive each grounded automatable-ask gap to a fix-and-merge under the umbrella.

    Detects + classifies every grounded ask cluster (via the default catalog
    classifier — the seam is injected at :func:`detect_automatable_asks` /
    :func:`classify_ask_cluster`), then routes each through
    :func:`teatree.loops.dream.umbrella_ledger.promote_gap`: a checkbox is upserted
    under the standing umbrella (deduped by ``cluster_key``) carrying the Bucket-A/B
    framing in its title, and a coding task is scheduled for the fix (linked back to
    the ask ``ConsolidatedMemory`` by ``cluster_key`` so reconcile-on-merge retires it).
    The banned-term / bare-reference withholding is enforced inside ``promote_gap``.
    Under *dry_run* nothing is written or scheduled. Returns one outcome per grounded
    ask gap (ungrounded clusters yield no outcome).
    """
    from teatree.loops.dream import umbrella_ledger  # noqa: PLC0415

    outcomes: list[AutomationAskOutcome] = []
    for finding in detect_automatable_asks(clusters, extract):
        if dry_run:
            continue
        gap_outcome = umbrella_ledger.promote_gap(
            host,
            umbrella_url=umbrella_url,
            gap=umbrella_ledger.GapSpec(
                gap_key=finding.cluster_key, title=_ask_title(finding), cluster_key=finding.cluster_key
            ),
        )
        outcomes.append(
            AutomationAskOutcome(
                cluster_key=finding.cluster_key,
                bucket=finding.bucket,
                filed=gap_outcome.scheduled or gap_outcome.checkbox_added,
                ticket_url=umbrella_url,
                withheld=gap_outcome.withheld,
                reason=gap_outcome.reason,
            )
        )
    return outcomes


def row_looks_like_ask(row: ConsolidatedMemory) -> bool:
    """True when a persisted consolidated rule reads like a recurring automatable ask.

    The distiller clusters repeated user-ask turns into a ``ConsolidatedMemory`` row;
    this keeps the ask-flavoured rows out of the neutral-lesson bulk by matching the
    SAME imperative/operational cues the detector uses (:data:`_ASK_CUES`) against the
    consolidated rule prose — so the phase promotes only ask-clusters.
    """
    lowered = row.rule.lower()
    return any(cue in lowered for cue in _ASK_CUES)


def cluster_for_row(row: ConsolidatedMemory) -> DistilledCluster:
    """Reconstruct a :class:`DistilledCluster` from a persisted ask-cluster row.

    The row already carries every field the grounding guard and the classifier need
    (``cluster_key``, ``rule``, ``source_files``, ``verified_citation``), so the
    promotion path reuses the exact same value type the engine distilled — no parallel
    representation.
    """
    return DistilledCluster(
        cluster_key=row.cluster_key,
        rule=row.rule,
        source_files=list(row.source_files),
        is_binding=row.is_binding,
        verified_citation=row.verified_citation,
        durable_destination=row.durable_destination,
    )


def run_automation_asks_phase(
    extract: ConsolidationExtract,
    host: CodeHostBackend,
    *,
    umbrella_url: str,
    dry_run: bool,
) -> str:
    """Promote every grounded persisted ask-cluster to a fix-and-merge (#2663).

    Reads the consolidated ask rows (those whose rule reads like a recurring ask,
    :func:`row_looks_like_ask`), reconstructs each into a :class:`DistilledCluster`,
    and routes the grounded ones (cited snippet present in *extract*) through
    :func:`promote_automatable_asks` — a deduped umbrella checkbox + a scheduled coding
    fix per ask, carrying the Bucket-A/B framing. Under *dry_run* nothing is promoted.
    Returns the dream-command summary clause (empty when no ask was promoted).
    """
    rows = [row for row in ConsolidatedMemory.objects.all() if row_looks_like_ask(row)]
    if not rows:
        return ""
    clusters = [cluster_for_row(row) for row in rows]
    outcomes = promote_automatable_asks(clusters, extract, host, umbrella_url=umbrella_url, dry_run=dry_run)
    filed = sum(1 for outcome in outcomes if outcome.filed)
    if not outcomes:
        return ""
    return f"; promoted {filed} automatable-ask fix(es)"


__all__ = [
    "AUTOMATION_CATALOG",
    "DREAM_AUTOMATION_LABEL",
    "AskBucket",
    "AskClassifier",
    "AutomationAskFinding",
    "AutomationAskOutcome",
    "CatalogEntry",
    "classify_ask_cluster",
    "cluster_for_row",
    "detect_automatable_asks",
    "promote_automatable_asks",
    "row_looks_like_ask",
    "run_automation_asks_phase",
]
