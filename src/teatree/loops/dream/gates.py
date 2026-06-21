"""§4 acceptance gates (a)-(f) for the dream consolidation pass (#2545, #1933 § 4).

The gates are what make a consolidation pass ANTI-VACUOUS. Phases 1-6 cluster,
distil, cross-link, re-index, and decay; the gates assert the pass actually
PRESERVED the lessons and ACTUALLY consolidated — so a do-nothing, delete-only,
or over-compressing pass is CAUGHT rather than silently stamped success.

The six gates (#1933 § 4):

*   (a) **retention** — every QA pair answerable BEFORE the pass is still
    answerable AFTER it. A delete-only pass that drops an answer fails.
*   (b) **interference** — the prior-session probe pass-rate must not regress —
    a new cluster must not corrupt an old answer.
*   (c) **consolidation-actually-happened** — net memory size REDUCED *or* the
    schema/cluster count INCREASED, AND every pruned index line has a confirmed
    durable home. A no-op pass (size unchanged, schema unchanged) fails; a prune
    with no durable home fails.
*   (d) **index-budget** — the rendered ``MEMORY.md`` is back under its line /
    byte load-warning threshold.
*   (e) **monotonicity** — two passes over a stable corpus must not LOWER the
    retention pass-rate.
*   (f) **no-loss audit trail** — every archived/pruned entry is recorded with a
    source + a durable destination, and the archived artifact actually exists
    (restorable).

The probe corpus is SEEDED from the memory set: one :class:`QaProbe` per memory
file, whose ``expected_answer`` is a signature line lifted from the file. A probe
is *answerable* against a :class:`MemorySnapshot` when that signature is still
findable (in any memory body OR the index — a lesson transferred into the index
still counts). This is the deterministic, LLM-free replay the gates run on; the
answerer is injectable so a richer (LLM) answerer can replace it later without
touching the gates.

:func:`persist_probe_results` writes the corpus to the migrated-but-formerly-dead
:class:`teatree.core.models.DreamQaProbe` (idempotent on ``probe_key``,
accumulating pass/run counts and marking a re-recorded probe ``is_prior_session``)
so the model is now live.

PURE w.r.t. the real ``~/.claude``: every gate takes explicit snapshots; tests
pass in-memory snapshots and a tmp archive dir. The command computes the
before/after snapshots around the pass and surfaces a failing report by marking
the run attempted-not-succeeded (staleness keeps firing).
"""

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from teatree.loops.dream.decay import ArchivedMemory

_INDEX_NAME = "MEMORY.md"
_HEADING_RE = re.compile(r"^#{1,6}\s")
#: The memory-file an index line POINTS AT — the ``](name.md)`` markdown link
#: target the re-index writes (``- [name.md](name.md) — summary``). Anchored on
#: the link href, not any ``.md`` token, so a ``.md`` filename merely mentioned
#: in the free-text summary never counts as the line's target — only a reworded
#: pointer to a still-present memory homes the line; a genuinely lost pointer
#: stays unhomed even if its summary name-drops a surviving memory.
_MEMORY_REF_RE = re.compile(r"]\(([\w.\-/]+\.md)\)")

#: Load-warning thresholds for the rendered ``MEMORY.md`` index (gate d). The
#: index is one short line per memory; past these the index has stopped being a
#: scannable pointer list and the consolidation pass has failed to keep it small.
#: Generous on purpose — they are a regression alarm, not a hard size cap.
INDEX_LINE_BUDGET = 900
INDEX_BYTE_BUDGET = 256 * 1024


#: How a probe is checked against a snapshot — injectable so a future LLM answerer
#: can replace the deterministic signature-match without touching the gates.
ProbeAnswerer = Callable[["QaProbe", "MemorySnapshot"], bool]


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    """An immutable view of a memory dir at one instant — the gate input.

    ``memories`` maps each memory file NAME (e.g. ``feedback_x.md``) to its full
    text; ``index_text`` is the rendered ``MEMORY.md``. The size accessors are
    pure derivations used by the consolidation + budget gates.
    """

    memories: Mapping[str, str]
    index_text: str

    @classmethod
    def build(cls, *, memories: Mapping[str, str], index_text: str = "") -> "MemorySnapshot":
        return cls(memories=dict(memories), index_text=index_text)

    @property
    def byte_size(self) -> int:
        """Total bytes across every memory body (the corpus weight)."""
        return sum(len(text.encode("utf-8")) for text in self.memories.values())

    @property
    def index_byte_size(self) -> int:
        return len(self.index_text.encode("utf-8"))

    @property
    def index_line_count(self) -> int:
        return sum(1 for line in self.index_text.splitlines() if line.strip())

    @property
    def index_lines(self) -> frozenset[str]:
        """The non-blank index lines (used to diff what a pass pruned)."""
        return frozenset(line.strip() for line in self.index_text.splitlines() if line.strip())

    def contains(self, needle: str) -> bool:
        """True iff *needle* (normalized) is findable in any memory body OR the index."""
        target = _normalize(needle)
        if not target:
            return False
        if target in _normalize(self.index_text):
            return True
        return any(target in _normalize(text) for text in self.memories.values())


@dataclass(frozen=True, slots=True)
class QaProbe:
    """One question / expected-answer pair replayed around a pass.

    ``expected_answer`` is a signature lifted from the source memory; the probe is
    *answerable* when that signature is still findable in a snapshot.
    """

    question: str
    expected_answer: str
    source_name: str

    @property
    def probe_key(self) -> str:
        """sha256 of the question — the idempotency anchor matching ``DreamQaProbe``."""
        return hashlib.sha256(self.question.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class GateResult:
    """One gate's verdict — its name, pass/fail, a human detail, and any regressions."""

    name: str
    passed: bool
    detail: str
    regressions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DreamQaReport:
    """The aggregate of all six gates — passes iff every gate passes."""

    gate_results: tuple[GateResult, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return all(g.passed for g in self.gate_results)

    def render(self) -> str:
        return "; ".join(f"{g.name} {'PASS' if g.passed else 'FAIL'} ({g.detail})" for g in self.gate_results)


def _normalize(text: str) -> str:
    return " ".join(text.split()).lower()


def _line_targets_live_memory(line: str, snapshot: MemorySnapshot) -> bool:
    """Whether a pruned index *line* still points at a memory file present after the pass.

    The re-index phase rewrites an index line whenever a curated summary is
    clipped/reworded — the line TEXT changes but the pointer (and its memory
    file) survives. Such a line is NOT a lost lesson, so it must count as homed;
    only a pointer to a memory that actually vanished stays unhomed.
    """
    return any(ref in snapshot.memories for ref in _MEMORY_REF_RE.findall(line))


def snapshot_memory_dir(memory_dir: Path) -> MemorySnapshot:
    """Read a memory dir into an immutable :class:`MemorySnapshot`.

    Reads every ``*.md`` (excluding ``MEMORY.md``, captured separately as the
    index). A missing dir is a clean empty snapshot. NEVER touches the real
    ``~/.claude`` beyond the explicit *memory_dir* the caller passes.
    """
    if not memory_dir.is_dir():
        return MemorySnapshot.build(memories={}, index_text="")
    memories: dict[str, str] = {}
    index_text = ""
    for md in sorted(memory_dir.glob("*.md")):
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        if md.name == _INDEX_NAME:
            index_text = text
        else:
            memories[md.name] = text
    return MemorySnapshot.build(memories=memories, index_text=index_text)


def _signature_line(text: str) -> str:
    """The first substantive prose line of a memory — its retention signature."""
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*").strip()
        if not line or _HEADING_RE.match(raw.strip()):
            continue
        if line.startswith(("name:", "summary:", "description:", "type:", "metadata:", "---")):
            continue
        return line
    return ""


def derive_probes(snapshot: MemorySnapshot) -> list[QaProbe]:
    """Seed one :class:`QaProbe` per memory, keyed on a signature line.

    The signature is the memory's first substantive prose line; the probe is
    answerable while that line is still findable in a snapshot. A memory with no
    substantive line yields no probe (nothing to retain).
    """
    probes: list[QaProbe] = []
    for name, text in sorted(snapshot.memories.items()):
        signature = _signature_line(text)
        if not signature:
            continue
        probes.append(
            QaProbe(question=f"What is the lesson recorded in {name}?", expected_answer=signature, source_name=name)
        )
    return probes


def probe_answerable(probe: QaProbe, snapshot: MemorySnapshot, answerer: ProbeAnswerer | None = None) -> bool:
    """Whether *probe* is answerable against *snapshot* (default: signature findable)."""
    if answerer is not None:
        return answerer(probe, snapshot)
    return snapshot.contains(probe.expected_answer)


def _pass_rate(probes: Sequence[QaProbe], snapshot: MemorySnapshot, answerer: ProbeAnswerer | None) -> float:
    if not probes:
        return 1.0
    answered = sum(1 for probe in probes if probe_answerable(probe, snapshot, answerer))
    return answered / len(probes)


class Gate:
    """The six §4 acceptance gates, grouped — each a pure verdict over snapshots.

    Static methods so a caller computes one gate in isolation (the tests do) while
    the suite reads as one cohesive contract. :func:`evaluate_gates` runs all six.
    """

    @staticmethod
    def retention(
        probes: Sequence[QaProbe],
        snapshot_before: MemorySnapshot,
        snapshot_after: MemorySnapshot,
        answerer: ProbeAnswerer | None = None,
    ) -> GateResult:
        """(a) Every probe answerable BEFORE the pass must still be answerable AFTER it."""
        pre_answerable = [p for p in probes if probe_answerable(p, snapshot_before, answerer)]
        lost = [p.source_name for p in pre_answerable if not probe_answerable(p, snapshot_after, answerer)]
        passed = not lost
        detail = "all pre-answerable probes retained" if passed else f"{len(lost)} probe(s) no longer answerable"
        return GateResult(name="retention", passed=passed, detail=detail, regressions=tuple(sorted(set(lost))))

    @staticmethod
    def interference(
        prior_probes: Sequence[QaProbe],
        snapshot_after: MemorySnapshot,
        *,
        prior_pass_rate: float,
        answerer: ProbeAnswerer | None = None,
    ) -> GateResult:
        """(b) The prior-session probe pass-rate must not regress below its recorded value."""
        now_rate = _pass_rate(prior_probes, snapshot_after, answerer)
        passed = now_rate >= prior_pass_rate
        detail = f"prior pass-rate {now_rate:.2f} vs recorded {prior_pass_rate:.2f}"
        return GateResult(name="interference", passed=passed, detail=detail)

    @staticmethod
    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def consolidation_happened(  # noqa: PLR0913 — each kwarg is one documented §4 gate-(c) input.
        snapshot_before: MemorySnapshot,
        snapshot_after: MemorySnapshot,
        *,
        schema_before: int,
        schema_after: int,
        homed_index_lines: set[str],
        clusters_recorded: int = 0,
    ) -> GateResult:
        """(c) Consolidation actually happened, AND every pruned index line is homed.

        Consolidation "happened" when ANY of: the memory set's net byte size
        REDUCED, the ledger schema/cluster count INCREASED, or this pass RECORDED
        clusters (``clusters_recorded > 0`` — distillation landed rules in the DB
        ledger even when the on-disk file set did not shrink). A do-nothing pass (no
        size drop, no schema growth, no clusters) fails. Independently, any index
        line the pass PRUNED must have a confirmed durable home — a prune with no
        home fails.

        A pruned line is homed when it is in *homed_index_lines* (the durable
        destination the caller supplies — e.g. a lesson still findable after the
        pass), OR it still points at a memory file that survived the pass. The
        latter case is the re-index merely rewording/clipping a curated summary:
        the line text changes but the pointer is not lost, so it must not count as
        a prune. Without it every summary clip looked like a lost prune and the
        pass never stamped success (#2545 staleness defect).
        """
        size_reduced = snapshot_after.byte_size < snapshot_before.byte_size
        schema_grew = schema_after > schema_before
        distilled = clusters_recorded > 0
        pruned_lines = snapshot_before.index_lines - snapshot_after.index_lines
        unhomed = sorted(
            line
            for line in pruned_lines
            if line not in homed_index_lines and not _line_targets_live_memory(line, snapshot_after)
        )
        consolidated = size_reduced or schema_grew or distilled
        passed = consolidated and not unhomed
        if not consolidated:
            detail = "no consolidation: no size reduction, no schema growth, no clusters recorded"
        elif unhomed:
            detail = f"{len(unhomed)} pruned index line(s) have no confirmed durable home"
        else:
            detail = "consolidation happened; all pruned lines homed"
        return GateResult(name="consolidation", passed=passed, detail=detail, regressions=tuple(unhomed))

    @staticmethod
    def index_budget(snapshot_after: MemorySnapshot) -> GateResult:
        """(d) The rendered ``MEMORY.md`` is back under its line + byte load-warning budget."""
        over_lines = snapshot_after.index_line_count > INDEX_LINE_BUDGET
        over_bytes = snapshot_after.index_byte_size > INDEX_BYTE_BUDGET
        passed = not (over_lines or over_bytes)
        detail = (
            f"index {snapshot_after.index_line_count} line(s) / {snapshot_after.index_byte_size} byte(s) "
            f"(budget {INDEX_LINE_BUDGET} / {INDEX_BYTE_BUDGET})"
        )
        return GateResult(name="index_budget", passed=passed, detail=detail)

    @staticmethod
    def monotonicity(*, pass_rate_first: float, pass_rate_second: float) -> GateResult:
        """(e) Two passes over a stable corpus must not LOWER the retention pass-rate."""
        passed = pass_rate_second >= pass_rate_first
        detail = f"run-1 {pass_rate_first:.2f} -> run-2 {pass_rate_second:.2f}"
        return GateResult(name="monotonicity", passed=passed, detail=detail)

    @staticmethod
    def no_loss_audit(archived: "Sequence[ArchivedMemory]") -> GateResult:
        """(f) Every archived entry records a source + a destination that actually exists."""
        broken = sorted(a.name for a in archived if not a.source or not a.destination or not a.destination.is_file())
        passed = not broken
        detail = "all archived entries restorable" if passed else f"{len(broken)} archived entry(ies) not restorable"
        return GateResult(name="no_loss_audit", passed=passed, detail=detail, regressions=tuple(broken))


# ast-grep-ignore: ac-django-no-complexity-suppressions
def evaluate_gates(  # noqa: PLR0913 — each kwarg is one documented §4 gate input, kwargs-only.
    *,
    snapshot_before: MemorySnapshot,
    snapshot_after: MemorySnapshot,
    schema_before: int,
    schema_after: int,
    homed_index_lines: set[str],
    prior_pass_rate: float,
    pass_rate_first: float,
    pass_rate_second: float,
    archived: "Sequence[ArchivedMemory]",
    clusters_recorded: int = 0,
    probes: Sequence[QaProbe] | None = None,
    prior_probes: Sequence[QaProbe] | None = None,
    answerer: ProbeAnswerer | None = None,
) -> DreamQaReport:
    """Run all six §4 gates and aggregate them into a :class:`DreamQaReport`.

    When *probes* / *prior_probes* are not supplied they are derived from the
    BEFORE snapshot (retention) and the prior corpus is taken as the same set —
    the caller wires the persisted prior-session corpus when one exists.
    """
    derived = probes if probes is not None else derive_probes(snapshot_before)
    prior = prior_probes if prior_probes is not None else derived
    return DreamQaReport(
        gate_results=(
            Gate.retention(derived, snapshot_before, snapshot_after, answerer),
            Gate.interference(prior, snapshot_after, prior_pass_rate=prior_pass_rate, answerer=answerer),
            Gate.consolidation_happened(
                snapshot_before,
                snapshot_after,
                schema_before=schema_before,
                schema_after=schema_after,
                homed_index_lines=homed_index_lines,
                clusters_recorded=clusters_recorded,
            ),
            Gate.index_budget(snapshot_after),
            Gate.monotonicity(pass_rate_first=pass_rate_first, pass_rate_second=pass_rate_second),
            Gate.no_loss_audit(archived),
        )
    )


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
    from teatree.core.models import DreamQaProbe  # noqa: PLC0415

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
    from teatree.core.models import DreamQaProbe  # noqa: PLC0415

    prior = list(DreamQaProbe.objects.prior_session_probes(overlay))
    if not prior:
        return 1.0, False
    return sum(p.last_pass_rate for p in prior) / len(prior), True


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
    persist: bool = True,
) -> DreamQaReport:
    """Run the §4 acceptance gates for one memory dir and persist the probe corpus.

    Wiring entry point for the dream command (#2545): derives probes from the
    BEFORE snapshot, reads the recorded prior-session pass-rate as the monotonicity
    / interference baseline, computes the durable-home set for the consolidation
    gate as the pruned index lines whose lesson is still findable in the AFTER
    snapshot (transfer-before-prune), runs all six gates, and — unless *persist* is
    off (dry-run) — records each probe's outcome to :class:`DreamQaProbe` (so the
    formerly-dead model is populated and the next pass has a prior baseline).
    """
    probes = derive_probes(snapshot_before)
    prior_rate, had_prior = _prior_pass_rate(overlay)
    now_rate = _pass_rate(probes, snapshot_after, None)
    pruned_lines = snapshot_before.index_lines - snapshot_after.index_lines
    homed_index_lines = {line for line in pruned_lines if snapshot_after.contains(line)}
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
        probes=probes,
    )
    if persist:
        persist_probe_results(probes, snapshot_after, overlay=overlay)
    return report


__all__ = [
    "INDEX_BYTE_BUDGET",
    "INDEX_LINE_BUDGET",
    "DreamQaReport",
    "Gate",
    "GateResult",
    "MemorySnapshot",
    "ProbeAnswerer",
    "QaProbe",
    "derive_probes",
    "evaluate_gates",
    "persist_probe_results",
    "probe_answerable",
    "run_acceptance_pass",
    "snapshot_memory_dir",
]
