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
memory. :func:`escalate_recurrences` files ONE deduped ``needs-triage`` ticket per
recurring rule that PRESCRIBES the structural fix (a PreToolUse/Stop gate, a
deterministic config self-check, or an anti-vacuous ``under_load`` eval) — it
never proposes writing more prose. That is the operationalisation of
``feedback_instruction_compliance_is_the_root_kpi``.

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

import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import InstructionComplianceRecord, InstructionComplianceSnapshot, RuleSource
from teatree.core.models.implemented_issue_marker import NEEDS_TRIAGE_LABEL
from teatree.core.review_findings import find_bare_references, neutralize_bare_references
from teatree.hooks import banned_terms_scanner
from teatree.loops.dream.engine import ConsolidationExtract, DistilledCluster, WeightedSnippet
from teatree.loops.dream.promote_memory import _CORE_DESTINATION_PREFIXES
from teatree.loops.dream.transcript_extract import looks_like_user_correction
from teatree.types import RawAPIDict

#: Where a reclassified recurring MEMORY_ONLY cluster is sent instead of a memory
#: file — a teatree-core path, so Pass-2 triage reads it as a core gap and files an
#: enforcement ticket rather than re-promoting another memory.
_RECURRENCE_CORE_DESTINATION = "src/teatree/loops/dream/compliance.py"

#: The dedup marker the escalation filer embeds (and searches for), keyed on the
#: recurring rule's identity, so a re-run never refiles a recurrence that already
#: has an open enforcement issue — mirrors the Pass-2 gap marker.
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
    """The result of escalating one recurring rule to an enforcement ticket.

    ``filed`` is True only when a NEW issue was created; ``ticket_url`` is the
    linked issue (newly filed OR a reused open dedup match); ``withheld`` is True
    when the rendered body would leak a banned term / bare reference.
    """

    rule_identity: str
    filed: bool
    ticket_url: str = ""
    withheld: bool = False
    reason: str = ""


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
    """A destination is MEMORY_ONLY when it is not a teatree-core fix path."""
    home = destination.strip().lower()
    return not (home and home.startswith(_CORE_DESTINATION_PREFIXES))


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
    reads it as a core gap and files an enforcement ticket instead of re-promoting a
    memory. A cluster already destined for a core path, or whose rule has no
    recurrence on record, is returned untouched.
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
    repo: str,
    snapshot: InstructionComplianceSnapshot | None = None,
    dry_run: bool = False,
) -> list[EscalationOutcome]:
    """File ONE deduped enforcement ticket per recurring rule — never a memory.

    Only recurrences (a rule that already had a durable memory, violated again)
    escalate; a first-occurrence finding files nothing. Two recurrences of the same
    rule collapse to one ticket (deduped by ``rule_identity``), and an open issue
    already carrying the recurrence marker is reused. The ticket PRESCRIBES the
    structural fix — a gate, a config self-check, or an anti-vacuous eval — and
    NEVER proposes writing another memory. When *snapshot* is supplied, the matching
    audit row is stamped escalated with the filed URL.
    """
    recurring = {f.rule_identity: f for f in findings if f.is_recurrence}
    outcomes: list[EscalationOutcome] = []
    for identity, finding in recurring.items():
        if dry_run:
            continue
        outcome = _file_one_escalation(host, finding, repo=repo)
        outcomes.append(outcome)
        if snapshot is not None and outcome.ticket_url:
            _stamp_escalated(snapshot, identity, outcome.ticket_url)
    return outcomes


def run_compliance_phase(
    *,
    since: datetime | None,
    dry_run: bool,
    host: CodeHostBackend | None,
    repo: str,
) -> str:
    """Detect → persist → escalate one pass's instruction-compliance failures.

    Builds the same bounded extract the engine distils, detects failures, persists
    one snapshot + audit rows, and (unless *dry_run*) escalates each recurrence to
    ONE deduped enforcement ticket via *host*. Returns the dream-command summary
    clause. A None *host* (no resolved backlog code host) reports a skip rather than
    raising. The phase enable/gating decision is the caller's; this runs the work.
    """
    from teatree.loops.dream import engine  # noqa: PLC0415

    extract = engine.build_extract(engine.enumerate_members(since=since))
    summary = build_compliance_snapshot(extract)
    snapshot = persist_compliance_pass(summary.findings, instructions_observed=summary.instructions_observed)
    filed = 0
    if not dry_run:
        if host is None:
            return "; WARN compliance escalation skipped — no teatree code host resolved"
        outcomes = escalate_recurrences(summary.findings, host, repo=repo, snapshot=snapshot, dry_run=dry_run)
        filed = sum(1 for o in outcomes if o.filed)
    if not summary.findings:
        return ""
    return (
        f"; compliance {summary.violations} violation(s)/{summary.recurrences_count} recurrence(s), escalated {filed}"
    )


def render_compliance_show() -> list[str]:
    """Render the latest compliance snapshot for `t3 dream compliance show`.

    Returns the lines to print: the rate + recurrence-count headline, then the open
    escalations (recurrences already routed to a filed gate/eval ticket), or a clear
    "nothing recorded yet" line when no pass has run.
    """
    snapshot = InstructionComplianceSnapshot.objects.latest_for()
    if snapshot is None:
        return ["No compliance snapshot recorded yet — run `t3 dream run --full` with compliance enabled."]
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


def _file_one_escalation(host: CodeHostBackend, finding: ComplianceFinding, *, repo: str) -> EscalationOutcome:
    """File (or reuse) one enforcement ticket for a recurring rule.

    Dedup-first: an open issue already carrying this rule's recurrence marker is
    reused. A body that would leak a banned term / bare reference is withheld.
    Otherwise a ``needs-triage`` issue prescribing the structural fix is filed.
    """
    existing = _find_existing_escalation(host, repo=repo, rule_identity=finding.rule_identity)
    if existing:
        return EscalationOutcome(
            rule_identity=finding.rule_identity, filed=False, ticket_url=existing, reason="reused open issue"
        )

    title = _escalation_title(finding)
    body = _escalation_body(finding)
    rendered = f"{title}\n{body}"

    banned = banned_terms_scanner.scan_text(rendered)
    if banned is not None:
        return EscalationOutcome(
            rule_identity=finding.rule_identity, filed=False, withheld=True, reason=f"contains banned term '{banned}'"
        )
    leaked = find_bare_references(rendered)
    if leaked:
        return EscalationOutcome(
            rule_identity=finding.rule_identity,
            filed=False,
            withheld=True,
            reason=f"contains bare reference(s): {', '.join(leaked)}",
        )

    raw = host.create_issue(repo=repo, title=title, body=body, labels=[_RECURRENCE_MARKER, NEEDS_TRIAGE_LABEL])
    return EscalationOutcome(
        rule_identity=finding.rule_identity, filed=True, ticket_url=_issue_url(raw), reason="filed"
    )


def _find_existing_escalation(host: CodeHostBackend, *, repo: str, rule_identity: str) -> str:
    marker = f"{_RECURRENCE_MARKER} {rule_identity}"
    try:
        matches = host.search_open_issues(repo=repo, query=marker)
    except Exception:  # noqa: BLE001 — a search hiccup must not block filing; refile-once self-corrects.
        return ""
    for raw in matches:
        body = str(raw.get("body") or raw.get("description") or "")
        if marker in body:
            return _issue_url(raw)
    return ""


def _escalation_title(finding: ComplianceFinding) -> str:
    return f"Compliance recurrence — enforce `{finding.rule_identity}` with a gate or eval"


def _escalation_body(finding: ComplianceFinding) -> str:
    evidence = neutralize_bare_references(finding.evidence.strip()) or "(no excerpt captured)"
    return (
        "A rule that ALREADY has a durable memory was violated AGAIN. Per the root-KPI "
        "rule, a recurrence-despite-memory must be remediated with a STRUCTURAL forcing "
        "function — never another memory (more prose is itself an instruction that will "
        "not be followed).\n\n"
        f"**Recurring rule:** `{finding.rule_identity}` (source: durable memory)\n\n"
        f"**Evidence this pass:** {evidence}\n\n"
        "**Prescribed structural fix (pick one):**\n"
        "- a PreToolUse/Stop gate that blocks the violating action on the actual write path, or\n"
        "- a deterministic config self-check that fails loud, or\n"
        "- an anti-vacuous `under_load` eval scenario with a `_fail` fixture that pins the behaviour.\n\n"
        f"<!-- {_RECURRENCE_MARKER} {finding.rule_identity} -->\n"
    )


def _issue_url(raw: RawAPIDict) -> str:
    for key in ("html_url", "web_url", "url"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


__all__ = [
    "ComplianceFinding",
    "ComplianceSnapshotResult",
    "EscalationOutcome",
    "build_compliance_snapshot",
    "detect_compliance_failures",
    "escalate_recurrences",
    "persist_compliance_pass",
    "reclassify_recurring_memory_clusters",
    "render_compliance_show",
    "run_compliance_phase",
]
