"""§4 acceptance gates (a)-(g) for the dream consolidation pass (#2545, #1933 § 4, #2663).

The gates are what make a consolidation pass ANTI-VACUOUS. Phases 1-6 cluster,
distil, cross-link, re-index, and decay; the gates assert the pass actually
PRESERVED the lessons and ACTUALLY consolidated — so a do-nothing, delete-only,
or over-compressing pass is CAUGHT rather than silently stamped success.

The seven gates (#1933 § 4; gate (g) added by #2663):

*   (a) **retention** — every QA pair answerable BEFORE the pass is still
    answerable AFTER it. A delete-only pass that drops an answer fails.
*   (b) **interference** — the prior-session probe pass-rate must not regress —
    a new cluster must not corrupt an old answer.
*   (c) **consolidation-actually-happened** — net memory size REDUCED *or* the
    schema/cluster count INCREASED, AND every pruned index line has a confirmed
    durable home. A no-op pass (size unchanged, schema unchanged) fails; a prune
    with no durable home fails.
*   (d) **index-budget** — the rendered ``MEMORY.md`` is back under its ~24 KB
    session-load BYTE budget (harness truncates by bytes; line count irrelevant; #2755).
*   (e) **monotonicity** — two passes over a stable corpus must not LOWER the
    retention pass-rate.
*   (f) **no-loss audit trail** — every archived/pruned entry is recorded with a
    source + a durable destination, and the archived artifact actually exists
    (restorable).
*   (g) **compliance-non-regression** (#2663) — a recurrence (a rule that already
    had a durable memory, violated again) remediated with ANOTHER memory instead
    of a gate/eval FAILS the pass; a pass that escalated every recurrence passes.

The probe corpus is SEEDED from the memory set: one :class:`QaProbe` per memory
file, whose ``expected_answer`` is a signature line lifted from the file. A probe
is *answerable* against a :class:`MemorySnapshot` when that signature is still
findable (in any memory body OR the index — a lesson transferred into the index
still counts). This is the deterministic, LLM-free replay the gates run on; the
answerer is injectable so a richer (LLM) answerer can replace it later without
touching the gates.

The impure WIRING around these gates — deriving the probe corpus, reading the
prior-session baseline, running the pass, and persisting each probe's replay to
:class:`teatree.core.models.DreamQaProbe` — lives in the sibling :mod:`acceptance`
module. This module stays PURE w.r.t. the real ``~/.claude``: every gate takes
explicit snapshots; tests pass in-memory snapshots and a tmp archive dir.
"""

import hashlib
import re
from collections.abc import Callable, Container, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from teatree.loops.dream import reindex

if TYPE_CHECKING:
    from teatree.loops.dream.decay import ArchivedMemory

_INDEX_NAME = "MEMORY.md"
#: The memory-file an index line POINTS AT — the LEADING filename pointer the
#: re-index writes at line start (``- name.md — summary``, or the legacy
#: ``- [name.md](name.md) — summary`` markdown-link form). Anchored on the
#: line-leading pointer position, NOT any ``.md`` token mid-line, so a ``.md``
#: filename merely mentioned in the free-text summary never counts as the line's
#: target — only a reworded pointer to a still-present memory homes the line; a
#: genuinely lost pointer stays unhomed even if its summary name-drops a
#: surviving memory.
_MEMORY_REF_RE = re.compile(r"^\s*-\s+\[?([\w.\-/]+\.md)\b")

#: Load budget for the rendered ``MEMORY.md`` index (gate d). The index is one
#: short line per memory and is read WHOLE at every session load; the harness
#: truncates it by BYTES at ~24 KB, so past that point the tail of the index never
#: reaches the agent and the consolidation pass has silently failed to keep memory
#: loadable. Bytes are the ONLY constraint — line count is irrelevant to what
#: reaches the agent, so a fixed line cap was a pessimistic proxy that forced
#: needless archival while byte headroom went unused (#2755). This tracks that real
#: session-load byte limit — NOT a 10x regression alarm — so an over-budget index
#: trips gate (d) RED while it is still recoverable (#2723).
INDEX_BYTE_BUDGET = 24 * 1024


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


def scoped_probe_key(scope: str, question: str) -> str:
    """The ``DreamQaProbe`` idempotency anchor: sha256 of scope + NUL + question.

    Folding the corpus *scope* (the memory dir) into the key keeps two dirs holding
    a same-named memory — hence the same question — on DISTINCT rows instead of
    colliding on one shared row.
    """
    return hashlib.sha256(f"{scope}\x00{question}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class QaProbe:
    """One question / expected-answer pair replayed around a pass.

    ``expected_answer`` is a signature lifted from the source memory; the probe is
    *answerable* when that signature is still findable in a snapshot.
    """

    question: str
    expected_answer: str
    source_name: str


@dataclass(frozen=True, slots=True)
class GateResult:
    """One gate's verdict — its name, pass/fail, a human detail, and any regressions."""

    name: str
    passed: bool
    detail: str
    regressions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ComplianceRemediationView:
    """One compliance violation's remediation, as the §4 gate (g) reads it (#2663).

    ``is_recurrence`` is True when the violated rule already had a durable memory;
    ``remediated_with_memory`` is True when the recorded remediation was ANOTHER
    memory — the forbidden non-fix for a recurrence that gate (g) FAILS on.
    """

    rule_identity: str
    is_recurrence: bool
    remediated_with_memory: bool


@dataclass(frozen=True, slots=True)
class DreamQaReport:
    """The aggregate of all seven §4 gates — passes iff every gate passes."""

    gate_results: tuple[GateResult, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return all(g.passed for g in self.gate_results)

    def render(self) -> str:
        return "; ".join(f"{g.name} {'PASS' if g.passed else 'FAIL'} ({g.detail})" for g in self.gate_results)


def _normalize(text: str) -> str:
    return " ".join(text.split()).lower()


def _line_targets(line: str, names: Container[str]) -> bool:
    """Whether a pruned index *line*'s leading ``.md`` pointer is one of *names*.

    The shared homing test — a pruned line is NOT a lost lesson when its pointer targets a
    memory still present after the pass (re-index merely reworded the summary) OR a file
    archived this pass (a restorable durable home in ``archive/`` + the cold
    ``MEMORY_ARCHIVE.md``, #2723, resolving the #2546 transfer-before-prune tension). Keys
    on the line-leading pointer only, never a ``.md`` token in the free-text summary.
    """
    return any(ref in names for ref in _MEMORY_REF_RE.findall(line))


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
    """The memory's retention signature — delegated to the frontmatter-aware extractor.

    Routes through :func:`reindex.signature_text` (#2746 nit-4) so the hot index, the
    cold ``MEMORY_ARCHIVE.md`` index, and the retention probe share ONE extractor that
    prefers the frontmatter ``description:`` over the weak ``node_type: memory`` body
    line. The returned line stays a substring of the memory's text, so
    ``snapshot.contains(signature)`` remains True (retention/interference stay green).
    ``reindex`` imports neither ``gates`` nor ``decay`` (stdlib only), so the
    module-level import edge adds no cycle.
    """
    return reindex.signature_text(text)


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
        maintenance_performed: bool = False,
        archived_names: Container[str] = (),
    ) -> GateResult:
        """(c) Consolidation actually happened, AND every pruned index line is homed.

        Consolidation "happened" when ANY of: the memory set's net byte size
        REDUCED, the ledger schema/cluster count INCREASED, this pass RECORDED
        clusters (``clusters_recorded > 0`` — distillation landed rules in the DB
        ledger even when the on-disk file set did not shrink), or the file-side
        maintenance phases did real work (``maintenance_performed`` — cross-link
        edges added, MEMORY.md re-indexed, or stale memories archived). A quiet-night
        pass that distils 0 NEW clusters yet cross-links / re-indexes / decays IS
        real consolidation maintenance and PASSES. A do-nothing pass (no size drop,
        no schema growth, no clusters, no maintenance) still fails. Independently,
        any index line the pass PRUNED must have a confirmed durable home — a prune
        with no home fails.

        A pruned line is homed when it is in *homed_index_lines* (the durable
        destination the caller supplies — e.g. a lesson still findable after the
        pass), OR it still points at a memory file that survived the pass. The
        latter case is the re-index merely rewording/clipping a curated summary:
        the line text changes but the pointer is not lost, so it must not count as
        a prune. Without it every summary clip looked like a lost prune and the
        pass never stamped success (#2545 staleness defect).

        A line targeting a memory the decay phase ARCHIVED (*archived_names*) is
        likewise homed: the archive IS its durable destination, and gate (f)
        independently fails any archived entry that is not restorable. Without
        this, phase 6 doing its job — archive a decayed memory, re-index drops its
        line — made a healthy pass fail gate (c), so the success marker was never
        stamped (souliane/teatree#3467).
        """
        size_reduced = snapshot_after.byte_size < snapshot_before.byte_size
        schema_grew = schema_after > schema_before
        distilled = clusters_recorded > 0
        pruned_lines = snapshot_before.index_lines - snapshot_after.index_lines
        unhomed = sorted(
            line
            for line in pruned_lines
            if line not in homed_index_lines
            and not _line_targets(line, snapshot_after.memories)
            and not _line_targets(line, archived_names)
        )
        consolidated = size_reduced or schema_grew or distilled or maintenance_performed
        passed = consolidated and not unhomed
        if not consolidated:
            detail = "no consolidation: no size reduction, no schema growth, no clusters recorded, no maintenance work"
        elif unhomed:
            detail = f"{len(unhomed)} pruned index line(s) have no confirmed durable home"
        else:
            detail = "consolidation happened; all pruned lines homed"
        return GateResult(name="consolidation", passed=passed, detail=detail, regressions=tuple(unhomed))

    @staticmethod
    def index_budget(snapshot_after: MemorySnapshot) -> GateResult:
        """(d) The rendered ``MEMORY.md`` is back under its ~24 KB session-load BYTE budget.

        Bytes are the only constraint — the harness truncates by bytes, so line count is
        irrelevant; a fixed line cap was a pessimistic proxy that wasted byte headroom (#2755).
        """
        over_bytes = snapshot_after.index_byte_size > INDEX_BYTE_BUDGET
        detail = (
            f"index {snapshot_after.index_byte_size} byte(s) / {snapshot_after.index_line_count} line(s) "
            f"(budget {INDEX_BYTE_BUDGET} bytes)"
        )
        return GateResult(name="index_budget", passed=not over_bytes, detail=detail)

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

    @staticmethod
    def compliance_non_regression(remediations: Sequence[ComplianceRemediationView]) -> GateResult:
        """(g) A recurrence remediated with a memory (instead of a gate/eval) FAILS the pass.

        The root-KPI rule (#2663): a rule that already has a durable memory and is
        violated AGAIN must escalate to a gate or an eval — writing another memory is
        itself an instruction that will not be followed. So a pass that OBSERVED a
        recurrence and recorded a MEMORY remediation for it regresses and fails; a
        pass with no recurrence, or one that escalated every recurrence, passes. A
        first-occurrence violation kept as a memory is legitimate and does not fail.
        """
        regressed = sorted(r.rule_identity for r in remediations if r.is_recurrence and r.remediated_with_memory)
        passed = not regressed
        detail = (
            "no recurrence remediated with a memory"
            if passed
            else f"{len(regressed)} recurrence(s) remediated with a memory instead of a gate/eval"
        )
        return GateResult(name="compliance_non_regression", passed=passed, detail=detail, regressions=tuple(regressed))


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
    maintenance_performed: bool = False,
    probes: Sequence[QaProbe] | None = None,
    prior_probes: Sequence[QaProbe] | None = None,
    answerer: ProbeAnswerer | None = None,
    compliance_remediations: Sequence[ComplianceRemediationView] = (),
) -> DreamQaReport:
    """Run all seven §4 gates and aggregate them into a :class:`DreamQaReport`.

    When *probes* / *prior_probes* are not supplied they are derived from the
    BEFORE snapshot (retention) and the prior corpus is taken as the same set —
    the caller wires the persisted prior-session corpus when one exists.
    *compliance_remediations* feeds gate (g): empty (the default) is a clean pass —
    no recurrence was remediated with a memory.
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
                maintenance_performed=maintenance_performed,
                archived_names={a.name for a in archived},
            ),
            Gate.index_budget(snapshot_after),
            Gate.monotonicity(pass_rate_first=pass_rate_first, pass_rate_second=pass_rate_second),
            Gate.no_loss_audit(archived),
            Gate.compliance_non_regression(compliance_remediations),
        )
    )


__all__ = [
    "INDEX_BYTE_BUDGET",
    "ComplianceRemediationView",
    "DreamQaReport",
    "Gate",
    "GateResult",
    "MemorySnapshot",
    "ProbeAnswerer",
    "QaProbe",
    "derive_probes",
    "evaluate_gates",
    "probe_answerable",
    "scoped_probe_key",
    "snapshot_memory_dir",
]
