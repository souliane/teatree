"""§4 acceptance-pass orchestration + persistence around the dream consolidation gates.

The pure gate predicates and their domain types live in :mod:`gates` (snapshot in,
verdict out, no DB). This module is the impure WIRING around them: it derives the
probe corpus, reads the recorded prior-session baseline, computes the durable-home
set from what decay archived, runs the seven gates via :func:`gates.evaluate_gates`,
and persists each probe's replay outcome to :class:`teatree.core.models.DreamQaProbe`.

Dependency direction is one-way — ``acceptance`` imports ``gates`` and ``decay``;
neither imports it back (``decay`` still reaches a couple of gate helpers
function-scoped, unchanged), so the split adds no import cycle.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from teatree.loops.dream import decay
from teatree.loops.dream.gates import (
    ComplianceRemediationView,
    DreamQaReport,
    MemorySnapshot,
    ProbeAnswerer,
    QaProbe,
    _line_targets,
    _pass_rate,
    derive_probes,
    evaluate_gates,
    probe_answerable,
)

if TYPE_CHECKING:
    from teatree.loops.dream.decay import ArchivedMemory


# ast-grep-ignore: ac-django-no-complexity-suppressions
def run_acceptance_pass(  # noqa: PLR0913 — kwargs-only §4 pass inputs; the command's per-dir gate entry point.
    snapshot_before: MemorySnapshot,
    snapshot_after: MemorySnapshot,
    *,
    overlay: str,
    archived: "Sequence[ArchivedMemory]",
    schema_before: int,
    schema_after: int,
    clusters_recorded: int = 0,
    maintenance_performed: bool = False,
    persist: bool = True,
    archive_dir: Path | None = None,
    compliance_remediations: Sequence[ComplianceRemediationView] | None = None,
) -> DreamQaReport:
    """Run the §4 acceptance gates for one memory dir and persist the probe corpus.

    Wiring entry point for the dream command (#2545): derives probes from the
    BEFORE snapshot, reads the recorded prior-session pass-rate as the monotonicity
    / interference baseline, computes the durable-home set for the consolidation
    gate as the pruned index lines whose lesson is still findable in the AFTER
    snapshot (transfer-before-prune) OR whose pointer targets a file in the durable
    ``archive/`` cold store (*archive_dir*, this/prior pass, #2723), threads the caller's *maintenance_performed*
    signal (file-side phases did real cross-link / re-index / decay work) into the
    consolidation gate so a quiet 0-cluster maintenance pass still counts as
    consolidation, runs all seven gates, and — unless *persist* is
    off (dry-run) — records each probe's outcome to :class:`DreamQaProbe` (so the
    formerly-dead model is populated and the next pass has a prior baseline).

    *compliance_remediations* feeds gate (g); when not supplied it is read from the
    persisted instruction-compliance records for *overlay* (#2663) so a recurrence
    remediated with a memory FAILS the pass.
    """
    probes = derive_probes(snapshot_before)
    prior_rate, had_prior = _prior_pass_rate(overlay)
    now_rate = _pass_rate(probes, snapshot_after, None)
    pruned_lines = snapshot_before.index_lines - snapshot_after.index_lines
    homed_names = {a.source.name for a in archived} | decay.cold_archive_names(archive_dir)
    homed_index_lines = {ln for ln in pruned_lines if snapshot_after.contains(ln) or _line_targets(ln, homed_names)}
    remediations = compliance_remediations if compliance_remediations is not None else _compliance_remediations(overlay)
    report = evaluate_gates(
        snapshot_before=snapshot_before,
        snapshot_after=snapshot_after,
        schema_before=schema_before,
        schema_after=schema_after,
        homed_index_lines=homed_index_lines,
        prior_pass_rate=prior_rate if had_prior else now_rate,
        pass_rate_first=prior_rate if had_prior else now_rate,
        pass_rate_second=now_rate,
        archived=archived,
        clusters_recorded=clusters_recorded,
        maintenance_performed=maintenance_performed,
        probes=probes,
        compliance_remediations=remediations,
    )
    if persist:
        persist_probe_results(probes, snapshot_after, overlay=overlay)
    return report


def persist_probe_results(
    probes: Sequence[QaProbe],
    snapshot: MemorySnapshot,
    *,
    overlay: str,
    answerer: ProbeAnswerer | None = None,
) -> int:
    """Record each probe's replay outcome to the ``DreamQaProbe`` corpus, idempotently.

    Idempotent on ``probe_key`` (sha256 of the question): a probe re-recorded on a
    later run finds its existing row and ACCUMULATES pass/run counts via
    :meth:`DreamQaProbe.record_result`, and is marked ``is_prior_session`` from the
    second recording on (it now carries over from an earlier run). Returns the
    number of probes that PASSED this replay.
    """
    from teatree.core.models import DreamQaProbe  # noqa: PLC0415 — deferred: ORM import needs the app registry

    passed = 0
    for probe in probes:
        answerable = probe_answerable(probe, snapshot, answerer)
        passed += int(answerable)
        row, created = DreamQaProbe.objects.get_or_create(
            probe_key=probe.probe_key,
            defaults={
                "question": probe.question,
                "expected_answer": probe.expected_answer,
                "source_memory_path": probe.source_name,
                "overlay": overlay,
            },
        )
        if not created and not row.is_prior_session:
            row.is_prior_session = True
            row.save(update_fields=["is_prior_session"])
        row.record_result(passed=answerable)
    return passed


def _prior_pass_rate(overlay: str) -> tuple[float, bool]:
    """Recorded prior-session pass-rate for *overlay* and whether a prior run exists.

    Averages ``last_pass_rate`` over the persisted prior-session probes. A fresh
    overlay (no prior probes) returns ``(1.0, False)`` — gate (b)/(e) cannot
    regress against a non-existent baseline, so the first run is never failed by
    interference/monotonicity.
    """
    from teatree.core.models import DreamQaProbe  # noqa: PLC0415 — deferred: ORM import needs the app registry

    prior = list(DreamQaProbe.objects.prior_session_probes(overlay))
    if not prior:
        return 1.0, False
    return sum(p.last_pass_rate for p in prior) / len(prior), True


def _compliance_remediations(overlay: str) -> list[ComplianceRemediationView]:
    """Build gate-(g) views from the latest persisted instruction-compliance records.

    Reads the most recent :class:`InstructionComplianceSnapshot` for *overlay* and
    maps each of its records to a :class:`ComplianceRemediationView`. A pass with no
    snapshot yet returns no views — gate (g) then cleanly passes (nothing observed).
    """
    from teatree.core.models import (  # noqa: PLC0415 — deferred: ORM import needs the app registry
        InstructionComplianceRecord,
        InstructionComplianceSnapshot,
        RemediationKind,
    )

    snapshot = InstructionComplianceSnapshot.objects.latest_for(overlay)
    if snapshot is None:
        return []
    return [
        ComplianceRemediationView(
            rule_identity=record.rule_identity,
            is_recurrence=record.is_recurrence,
            remediated_with_memory=record.remediation == RemediationKind.MEMORY,
        )
        for record in InstructionComplianceRecord.objects.filter(snapshot=snapshot)
    ]


__all__ = [
    "persist_probe_results",
    "run_acceptance_pass",
]
