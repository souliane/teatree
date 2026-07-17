"""Dream phase 3c — the Instruction-Compliance Accountant (#2663).

Instruction compliance is the root KPI. The recurring complaint "you do NOT
follow instructions" is one failure mode wearing many masks: a rule was PRESENT
or AVAILABLE — a durable memory, a loaded-skill rule, a CLAUDE.md clause, a
system gate, or an explicit in-session user directive — and the agent acted
against it. This phase mines one dream pass's extract for those failures, models
each as a typed :class:`ComplianceFinding`, persists a snapshot + audit rows, and
ENFORCES the binding escalation rule.

THE BINDING RULE. When a rule that ALREADY has a durable memory is violated AGAIN
(``is_recurrence``), the remediation MUST be a gate or an eval, NEVER another
memory. :func:`escalate_recurrences` drives ONE deduped umbrella checkbox +
scheduled coding task per recurring rule (via ``umbrella_ledger.promote_gap``) that
PRESCRIBES the structural fix (a PreToolUse/Stop gate, a deterministic config
self-check, or an anti-vacuous ``under_load`` eval) and carries it to a MERGED fix
under the standing umbrella issue — it never proposes writing more prose. That is
the operationalisation of ``feedback_instruction_compliance_is_the_root_kpi``.

The detector reuses :func:`teatree.loops.dream.transcript_extract.looks_like_user_correction`
rather than re-implementing correction detection: a user-correction turn is the
ground-truth signal that the agent acted against an instruction. A correction
whose subject overlaps a memory-backed rule already on disk is a RECURRENCE
(``rule_source=MEMORY``); a correction with no backing memory is a first-occurrence
in-session directive violation (``rule_source=IN_SESSION``), still a compliance
failure but not yet an escalation trigger.

PURE w.r.t. the forge: filing goes through the injected
:class:`~teatree.core.backend_protocols.CodeHostBackend`, so the whole phase is
testable without an LLM and without a live forge. The filing gate mirrors
:mod:`teatree.loops.dream.promote_memory` (dedup-by-marker, banned-term / bare-ref
withholding) so an escalation ticket can never leak a banned term.
"""

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import InstructionComplianceRecord, InstructionComplianceSnapshot, RuleSource
from teatree.loops.dream.engine import ConsolidationExtract, DistilledCluster, WeightedSnippet
from teatree.loops.dream.promote_memory import UMBRELLA_ISSUE_URL, points_at_core_fix
from teatree.loops.dream.transcript_extract import looks_like_user_correction

logger = logging.getLogger(__name__)

#: Where a reclassified recurring MEMORY_ONLY cluster is sent instead of a memory
#: file — a teatree-core path, so Pass-2 triage reads it as a core gap and drives an
#: umbrella checkbox + scheduled gate/eval fix rather than re-promoting another memory.
_RECURRENCE_CORE_DESTINATION = "src/teatree/loops/dream/compliance.py"

#: The gap-key namespace for a compliance recurrence on the umbrella ledger, keyed
#: on the recurring rule's identity, so a re-run upserts the same checkbox / reuses
#: the same scheduled fix instead of double-adding — mirrors the Pass-2 gap key.
_RECURRENCE_MARKER = "compliance-recurrence"

#: Tokens shorter than this carry no topical signal — a correction sharing only
#: "the"/"not" with a memory is not a recurrence of that memory's rule.
_MIN_TOKEN_LEN = 5

#: Words that are frequent in BOTH memory bodies and correction prose and so are
#: non-discriminating — they must not, on their own, match a correction to a memory.
_STOPWORDS = frozenset(
    {
        "again",
        "always",
        "never",
        "should",
        "would",
        "their",
        "there",
        "these",
        "those",
        "which",
        "while",
        "about",
        "instruction",
        "instructions",
        "follow",
        "memory",
        "rule",
        "feedback",
        "binding",
    }
)

_WORD_RE = re.compile(r"[a-z][a-z0-9_]+")
_NAME_LINE_RE = re.compile(r"^name:\s*(?P<slug>[\w\-]+)", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class ComplianceFinding:
    """One detected instruction-compliance failure — the typed phase-3c record.

    ``rule_source`` is where the violated rule was PRESENT/AVAILABLE;
    ``rule_identity`` is the stable handle the escalation rule dedups on (a memory
    slug for a recurrence, a normalised directive key otherwise);
    ``is_recurrence`` is True when the rule already had a durable memory and was
    violated AGAIN — the binding escalation trigger.
    """

    rule_source: RuleSource
    rule_identity: str
    evidence: str
    is_recurrence: bool


@dataclass(frozen=True, slots=True)
class ComplianceSnapshotResult:
    """The detector's pass summary: the persisted-metric inputs + the findings."""

    instructions_observed: int
    violations: int
    recurrences_count: int
    findings: tuple[ComplianceFinding, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class EscalationOutcome:
    """The result of driving one recurring rule onto the standing umbrella issue.

    ``filed`` is True when a new umbrella checkbox was added OR a coding task was
    scheduled (the ``promote_gap`` outcome); ``ticket_url`` is the umbrella issue URL;
    ``withheld`` is True when the rendered body would leak a banned term / bare
    reference.
    """

    rule_identity: str
    filed: bool
    ticket_url: str = ""
    withheld: bool = False
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ComplianceMeasurement:
    """One MEASUREMENT pass's result: the (maybe-persisted) snapshot + its findings.

    ``snapshot`` is the persisted :class:`InstructionComplianceSnapshot`, or ``None``
    when the pass observed 0 instructions (nothing to measure) or ran ``dry_run``.
    ``findings`` are carried forward so a subsequent ESCALATION pass (``--full`` +
    toggle) can act on the recurrences without recomputing. ``summary`` is the
    dream-command clause (empty when there were no violations to report).
    """

    snapshot: InstructionComplianceSnapshot | None
    findings: tuple[ComplianceFinding, ...]
    summary: str


@dataclass(frozen=True, slots=True)
class _MemoryRule:
    slug: str
    tokens: frozenset[str]


def _significant_tokens(text: str) -> set[str]:
    return {word for word in _WORD_RE.findall(text.lower()) if len(word) >= _MIN_TOKEN_LEN and word not in _STOPWORDS}


def _memory_slug(snippet: WeightedSnippet) -> str:
    match = _NAME_LINE_RE.search(snippet.text)
    if match:
        return match.group("slug")
    return snippet.path.stem


def _memory_rules(extract: ConsolidationExtract) -> list[_MemoryRule]:
    """Every memory snippet as a (slug, distinctive-tokens) rule available this pass."""
    rules: list[_MemoryRule] = []
    for snippet in extract.snippets:
        if snippet.kind != "memory":
            continue
        slug = _memory_slug(snippet)
        tokens = _significant_tokens(snippet.text) | _significant_tokens(slug)
        rules.append(_MemoryRule(slug=slug, tokens=frozenset(tokens)))
    return rules


def _correction_lines(extract: ConsolidationExtract) -> list[str]:
    """Every user-correction line across the transcript snippets (LLM-free ground truth)."""
    lines: list[str] = []
    for snippet in extract.snippets:
        if snippet.kind == "memory":
            continue
        lines.extend(line for line in snippet.text.splitlines() if looks_like_user_correction(line))
    return lines


def _directive_identity(line: str) -> str:
    """A stable identity for an in-session directive violation with no backing memory."""
    tokens = sorted(_significant_tokens(line))[:4]
    return "-".join(tokens) if tokens else "in-session-directive"


def _backing_memory(line: str, memory_rules: Sequence[_MemoryRule]) -> _MemoryRule | None:
    """The memory rule whose distinctive tokens the correction line shares, if any."""
    line_tokens = _significant_tokens(line)
    for rule in memory_rules:
        if rule.tokens & line_tokens:
            return rule
    return None


def detect_compliance_failures(extract: ConsolidationExtract) -> list[ComplianceFinding]:
    """Detect instruction-compliance failures in one pass's extract.

    A user-correction turn (via :func:`looks_like_user_correction`) is the
    ground-truth signal the agent acted against an instruction. A correction whose
    distinctive subject overlaps a memory-backed rule present in the extract is a
    RECURRENCE (``rule_source=MEMORY``, ``is_recurrence=True``) — the rule already
    had a durable memory and was violated again. A correction with no backing
    memory is a first-occurrence in-session directive violation
    (``rule_source=IN_SESSION``). De-duplicated by rule identity within the pass.
    """
    memory_rules = _memory_rules(extract)
    findings: dict[str, ComplianceFinding] = {}
    for line in _correction_lines(extract):
        backing = _backing_memory(line, memory_rules)
        if backing is not None:
            findings.setdefault(
                backing.slug,
                ComplianceFinding(
                    rule_source=RuleSource.MEMORY,
                    rule_identity=backing.slug,
                    evidence=line.strip()[:500],
                    is_recurrence=True,
                ),
            )
            continue
        identity = _directive_identity(line)
        findings.setdefault(
            identity,
            ComplianceFinding(
                rule_source=RuleSource.IN_SESSION,
                rule_identity=identity,
                evidence=line.strip()[:500],
                is_recurrence=False,
            ),
        )
    return list(findings.values())


def build_compliance_snapshot(extract: ConsolidationExtract) -> ComplianceSnapshotResult:
    """Detect failures and summarise them into the persisted-metric inputs.

    ``instructions_observed`` counts the rules in play this pass — every memory
    rule available plus every distinct directive a correction names — so the rate
    is violations against the instruction surface actually exercised, never a
    vacuous 1.0 from observing nothing.
    """
    findings = detect_compliance_failures(extract)
    memory_rules = _memory_rules(extract)
    directive_count = sum(1 for f in findings if f.rule_source is RuleSource.IN_SESSION)
    instructions_observed = len(memory_rules) + directive_count
    recurrences = sum(1 for f in findings if f.is_recurrence)
    return ComplianceSnapshotResult(
        instructions_observed=instructions_observed,
        violations=len(findings),
        recurrences_count=recurrences,
        findings=tuple(findings),
    )


def _recurring_rule_slugs() -> set[str]:
    """Every rule identity that has a recorded MEMORY-backed recurrence."""
    return set(
        InstructionComplianceRecord.objects.filter(is_recurrence=True, rule_source=RuleSource.MEMORY).values_list(
            "rule_identity", flat=True
        )
    )


def _is_memory_only(destination: str) -> bool:
    """A destination is MEMORY_ONLY when it is not a teatree-core fix path.

    Delegates to the shared :func:`~teatree.loops.dream.promote_memory.points_at_core_fix`
    classifier so the "is this a core-fix path?" rule has ONE home, not a copy here
    and another inline in Pass-2 triage.
    """
    return not points_at_core_fix(destination)


def _cluster_slugs(cluster: DistilledCluster) -> set[str]:
    """The memory slugs a cluster cites — the stems of its source memory files."""
    return {Path(str(path)).stem for path in cluster.source_files if str(path).strip()}


def reclassify_recurring_memory_clusters(
    clusters: Sequence[DistilledCluster],
) -> list[DistilledCluster]:
    """Redirect a MEMORY_ONLY cluster whose rule already recurred off the memory destination.

    The binding rule: a rule that already has a durable memory and recurs must NOT
    produce ANOTHER memory. So a cluster destined for a memory file
    (:func:`_is_memory_only`) whose cited slug already shows a recurrence in the
    audit ledger is reclassified to a teatree-core destination — Pass-2 triage then
    reads it as a core gap and drives an umbrella checkbox + scheduled gate/eval fix
    instead of re-promoting a memory. A cluster already destined for a core path, or
    whose rule has no recurrence on record, is returned untouched.
    """
    recurring = _recurring_rule_slugs()
    if not recurring:
        return list(clusters)
    out: list[DistilledCluster] = []
    for cluster in clusters:
        if _is_memory_only(cluster.durable_destination) and (_cluster_slugs(cluster) & recurring):
            out.append(replace(cluster, durable_destination=_RECURRENCE_CORE_DESTINATION))
        else:
            out.append(cluster)
    return out


def persist_compliance_pass(
    findings: Sequence[ComplianceFinding],
    *,
    instructions_observed: int,
    overlay: str = "",
) -> InstructionComplianceSnapshot:
    """Persist one pass's snapshot + one audit row per finding.

    The snapshot computes ``compliance_rate`` from the counts; each finding lands
    as an :class:`InstructionComplianceRecord` linked to it, so the recurrence
    audit trail survives the pass for the §4 gate (g) and the CLI to read.
    """
    recurrences = sum(1 for f in findings if f.is_recurrence)
    snapshot = InstructionComplianceSnapshot.record(
        instructions_observed=instructions_observed,
        violations=len(findings),
        recurrences_count=recurrences,
        overlay=overlay,
    )
    InstructionComplianceRecord.objects.bulk_create(
        InstructionComplianceRecord(
            snapshot=snapshot,
            rule_source=finding.rule_source,
            rule_identity=finding.rule_identity,
            evidence=finding.evidence,
            is_recurrence=finding.is_recurrence,
            overlay=overlay,
        )
        for finding in findings
    )
    return snapshot


def escalate_recurrences(
    findings: Sequence[ComplianceFinding],
    host: CodeHostBackend,
    *,
    umbrella_url: str = UMBRELLA_ISSUE_URL,
    snapshot: InstructionComplianceSnapshot | None = None,
    dry_run: bool = False,
) -> list[EscalationOutcome]:
    """Drive ONE umbrella checkbox + scheduled gate/eval fix per recurring rule (#2663).

    Only recurrences (a rule that already had a durable memory, violated again)
    escalate; a first-occurrence finding does nothing. Two recurrences of the same
    rule collapse to one gap (deduped by ``rule_identity``). Each recurrence rides the
    standing umbrella (*umbrella_url*) as a checkbox + a scheduled coding task whose
    title PRESCRIBES the structural fix — a gate, a config self-check, or an
    anti-vacuous eval — and NEVER proposes writing another memory; it no longer files a
    fresh ``needs-triage`` issue that the scanner skips. When *snapshot* is supplied,
    the matching audit row is stamped escalated with the umbrella URL.
    """
    recurring = {f.rule_identity: f for f in findings if f.is_recurrence}
    outcomes: list[EscalationOutcome] = []
    for identity, finding in recurring.items():
        outcome = _escalate_one_recurrence(host, finding, umbrella_url=umbrella_url, dry_run=dry_run)
        outcomes.append(outcome)
        if not dry_run and snapshot is not None and outcome.ticket_url:
            _stamp_escalated(snapshot, identity, outcome.ticket_url)
    return outcomes


def run_compliance_measurement(
    *,
    extract: ConsolidationExtract,
    dry_run: bool,
    overlay: str = "",
) -> ComplianceMeasurement:
    """MEASURE one pass's instruction compliance — persist a snapshot, never file (#2663).

    The root-KPI measurement runs on EVERY dream pass (default ON): it detects
    failures over the already-built *extract* the engine distils and PERSISTS one
    snapshot + audit rows so ``t3 dream compliance show`` and gate (g) can read the
    trend. It does NOT escalate — that is the separate ``--full``-gated
    :func:`run_compliance_escalation`. A pass that observed 0 instructions (an empty
    or memory-less extract) has nothing to measure, so it records NOTHING and returns
    a ``None`` snapshot with a WARNING. Under *dry_run* the tally is computed but no
    row is persisted. Returns a :class:`ComplianceMeasurement` carrying the snapshot,
    the findings (for a downstream escalation pass), and the summary clause.
    """
    summary = build_compliance_snapshot(extract)
    if summary.instructions_observed == 0:
        logger.warning("dream compliance measurement observed 0 instructions this pass — recording no snapshot.")
        return ComplianceMeasurement(snapshot=None, findings=summary.findings, summary="")
    snapshot = (
        None
        if dry_run
        else persist_compliance_pass(
            summary.findings, instructions_observed=summary.instructions_observed, overlay=overlay
        )
    )
    clause = (
        f"; compliance {summary.violations} violation(s)/{summary.recurrences_count} recurrence(s)"
        if summary.violations
        else ""
    )
    return ComplianceMeasurement(snapshot=snapshot, findings=summary.findings, summary=clause)


def run_compliance_escalation(
    *,
    snapshot: InstructionComplianceSnapshot | None,
    findings: Sequence[ComplianceFinding],
    host: CodeHostBackend | None,
    dry_run: bool,
) -> str:
    """ESCALATE each recurrence to a fix-and-merge under the standing umbrella (#2663).

    The default-OFF, ``--full``-gated other half of phase 3c: only recurrences (a
    memory-backed rule violated AGAIN) escalate, each riding one deduped umbrella
    checkbox + scheduled gate/eval coding task via *host* — never another memory.
    A ``None`` *host* (no resolved backlog code host) reports a skip rather than
    raising. Under *dry_run* nothing is filed. When *snapshot* is supplied, the
    matching audit row is stamped escalated. Returns the dream-command summary clause.
    """
    recurrences = sum(1 for f in findings if f.is_recurrence)
    if not recurrences:
        return ""
    if host is None:
        return "; WARN compliance escalation skipped — no teatree code host resolved"
    outcomes = escalate_recurrences(findings, host, snapshot=snapshot, dry_run=dry_run)
    filed = sum(1 for o in outcomes if o.filed)
    return f"; escalated {filed}/{recurrences} compliance recurrence(s)"


def render_compliance_show() -> list[str]:
    """Render the latest compliance snapshot for `t3 dream compliance show`.

    Returns the lines to print: the rate + recurrence-count headline, then the open
    escalations (recurrences already routed to a filed gate/eval ticket), or a clear
    "nothing recorded yet" line when no pass has run.
    """
    snapshot = InstructionComplianceSnapshot.objects.latest_for()
    if snapshot is None:
        return ["No compliance snapshot recorded yet — run `t3 dream run` (measurement is on by default)."]
    headline = (
        f"Instruction-compliance — rate {snapshot.compliance_rate:.2f} "
        f"({snapshot.violations} violation(s), {snapshot.recurrences_count} recurrence(s)) "
        f"as of {snapshot.created_at.isoformat()}"
    )
    lines = [headline]
    escalations = list(InstructionComplianceRecord.objects.open_escalations())
    if not escalations:
        lines.append("Open escalations: none.")
        return lines
    lines.append(f"Open escalations ({len(escalations)}):")
    lines.extend(f"  - {record.rule_identity} -> {record.escalation_url}" for record in escalations)
    return lines


def _stamp_escalated(snapshot: InstructionComplianceSnapshot, rule_identity: str, ticket_url: str) -> None:
    row = InstructionComplianceRecord.objects.filter(
        snapshot=snapshot, rule_identity=rule_identity, is_recurrence=True
    ).first()
    if row is not None:
        row.mark_escalated(ticket_url)


def _escalate_one_recurrence(
    host: CodeHostBackend, finding: ComplianceFinding, *, umbrella_url: str, dry_run: bool = False
) -> EscalationOutcome:
    """Drive one recurring rule to a fix-and-merge via the umbrella ledger (#2663).

    Reuses :func:`teatree.loops.dream.umbrella_ledger.promote_gap`: a checkbox is
    upserted under the umbrella (deduped by this recurrence's gap key) and a coding
    task is scheduled (deduped by the same key). The checkbox title PRESCRIBES the
    structural fix — a gate or an eval — never another memory. The banned-term /
    bare-reference withholding is enforced inside ``promote_gap`` (and still runs
    under *dry_run*, so a withheld gap is withheld in the preview too). Under
    *dry_run* nothing is written, but a non-withheld gap is reported as filed so the
    preview counts what a real run WOULD escalate rather than reporting zero.
    """
    from teatree.loops.dream import umbrella_ledger  # noqa: PLC0415 — deferred: loaded at tick time, not import

    gap_key = f"{_RECURRENCE_MARKER}-{finding.rule_identity}"
    outcome = umbrella_ledger.promote_gap(
        host,
        umbrella_url=umbrella_url,
        gap=umbrella_ledger.GapSpec(gap_key=gap_key, title=_escalation_title(finding), cluster_key=gap_key),
        dry_run=dry_run,
    )
    if outcome.withheld:
        return EscalationOutcome(rule_identity=finding.rule_identity, filed=False, withheld=True, reason=outcome.reason)
    return EscalationOutcome(
        rule_identity=finding.rule_identity,
        filed=outcome.scheduled or outcome.checkbox_added or dry_run,
        ticket_url=umbrella_url,
        reason=outcome.reason,
    )


def _escalation_title(finding: ComplianceFinding) -> str:
    return f"Compliance recurrence — enforce `{finding.rule_identity}` with a gate or eval"


__all__ = [
    "ComplianceFinding",
    "ComplianceMeasurement",
    "ComplianceSnapshotResult",
    "EscalationOutcome",
    "build_compliance_snapshot",
    "detect_compliance_failures",
    "escalate_recurrences",
    "persist_compliance_pass",
    "reclassify_recurring_memory_clusters",
    "render_compliance_show",
    "run_compliance_escalation",
    "run_compliance_measurement",
]
