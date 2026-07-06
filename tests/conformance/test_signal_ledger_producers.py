"""SIGNAL_LEDGER_PRODUCERS — the signal-has-a-live-writer conformance CAPSTONE (SIG-4).

The cluster's defining failure was producer/consumer registry drift: a signal
*reads* a ledger that no production path *writes*, so the reading is structurally
dead (S1 ``INSTRUMENTATION_GAP`` forever, S2 vacuous ``OK``) yet CI stays green
because the queries have their own fixture-injected rows. SIG-2 populated
``RedMrFixAttempt`` and SIG-3 wrote ``Ticket.Kind.FIX`` precisely because those
two ledgers had no writer.

This lane is the merge gate that certifies the WHOLE signal layer is alive end to
end. It enumerates every ledger model :mod:`teatree.core.factory.factory_signal_queries`
reads and asserts each maps to a LIVE producer in ``SIGNAL_LEDGER_PRODUCERS`` — a
declared write entry point whose source actually performs the write, plus an
exercised integration test. Two directions, both fail loud. First: a ledger read
with no registered producer (a NEW signal reading an unwritten ledger) — the
vacuity class SIG-2/SIG-3 fought — fails here instead of shipping. Second: a
registered producer that has been stubbed/gutted (its write removed, e.g. the
``claim_red_mr_fix`` fail-open regression #7 that returned ``True`` without ever
calling ``RedMrFixAttempt.claim``) fails here, NAMING the orphaned ledger.

Enumeration is by namespace introspection of the query module, so a producer
proof is required for the models it actually imports — the registry cannot silently
lag a newly-read ledger. It depends on SIG-2's and SIG-3's producers being on main;
without them the ``RedMrFixAttempt`` / ``Ticket`` lanes would (correctly) be red.
"""

import dataclasses
import importlib
import inspect
from pathlib import Path
from typing import ClassVar

from django.db.models import Model

from teatree.core.factory import factory_signal_queries

_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclasses.dataclass(frozen=True)
class SignalLedgerProducer:
    """One ledger's live-writer proof: the model, its writer, and the test that exercises it.

    ``write_entry_point`` is the dotted path to the production function/method
    that writes the ledger; ``write_call`` is the write expression that MUST
    appear in that function's own source (the stub-detector — gutting the writer
    removes it). ``integration_test`` is the repo-relative test that exercises the
    write end to end and must reference the ledger by name.
    """

    ledger: str
    write_entry_point: str
    write_call: str
    integration_test: str


# Every ledger model `factory_signal_queries` reads -> its live producer proof.
# Keyed by the model class name the enumeration below discovers in the query
# module's namespace, so a newly-imported ledger with no entry fails the coverage
# lane rather than shipping a dead read.
SIGNAL_LEDGER_PRODUCERS: tuple[SignalLedgerProducer, ...] = (
    SignalLedgerProducer(
        ledger="MergeAudit",
        write_entry_point="teatree.core.merge.execution.record_merge_and_advance",
        write_call="merge_audit_model.objects.create",
        integration_test="tests/teatree_core/test_merge_execution.py",
    ),
    SignalLedgerProducer(
        ledger="MergeClear",
        write_entry_point="teatree.core.models.merge_clear.MergeClear.issue",
        write_call="cls.objects.create",
        integration_test="tests/teatree_core/test_clear_issuance_and_human_substrate.py",
    ),
    SignalLedgerProducer(
        ledger="RedMrFixAttempt",
        write_entry_point="teatree.loop.dispatch_gates.claim_red_mr_fix",
        write_call="RedMrFixAttempt.claim",
        integration_test="tests/teatree_loop/test_sig2_red_mr_ledger.py",
    ),
    SignalLedgerProducer(
        ledger="Ticket",
        write_entry_point="teatree.core.ticket_kind_classification.classify_ticket_kind",
        write_call="Ticket.Kind.FIX",
        integration_test="tests/teatree_core/test_ticket_kind_classification.py",
    ),
    SignalLedgerProducer(
        ledger="TicketTransition",
        write_entry_point="teatree.core.signals._log_ticket_transition",
        write_call="TicketTransition.objects.create",
        integration_test="tests/teatree_core/test_transition.py",
    ),
    SignalLedgerProducer(
        ledger="ReviewVerdict",
        write_entry_point="teatree.core.models.review_verdict.ReviewVerdict.record",
        write_call="cls.objects.create",
        integration_test="tests/teatree_core/test_review_verdict_model.py",
    ),
    SignalLedgerProducer(
        ledger="TaskAttempt",
        write_entry_point="teatree.agents.attempt_recorder.record_result_envelope",
        write_call="TaskAttempt.objects.create",
        integration_test="tests/teatree_agents/test_attempt_recorder.py",
    ),
    SignalLedgerProducer(
        ledger="RedCardSignal",
        write_entry_point="teatree.core.models.red_card_signal.RedCardSignal.record",
        write_call="cls.objects.get_or_create",
        integration_test="tests/teatree_loop/test_red_card_scanner.py",
    ),
)


def ledger_models_read_by_signal_queries() -> dict[str, type[Model]]:
    """The concrete Django ledger models imported into the query module's namespace.

    Introspection, not a hand-list: any ``Model`` subclass the queries import is a
    ledger they read, so the coverage assertion below cannot silently lag a newly
    read ledger. Abstract bases are excluded (they carry no rows to write).
    """
    return {
        name: obj
        for name, obj in vars(factory_signal_queries).items()
        if isinstance(obj, type) and issubclass(obj, Model) and not obj._meta.abstract
    }


def _resolve(dotted: str) -> object:
    module_path, _, attr_path = dotted.partition(":") if ":" in dotted else _split_module_attr(dotted)
    obj: object = importlib.import_module(module_path)
    for part in attr_path.split("."):
        obj = getattr(obj, part)
    return obj


def _split_module_attr(dotted: str) -> tuple[str, str, str]:
    # A producer path is `pkg.mod.func` or `pkg.mod.Class.method`; split at the
    # last module boundary by importing the longest importable prefix.
    parts = dotted.split(".")
    for cut in range(len(parts) - 1, 0, -1):
        module_path = ".".join(parts[:cut])
        try:
            importlib.import_module(module_path)
        except ModuleNotFoundError:
            continue
        return module_path, ".", ".".join(parts[cut:])
    msg = f"no importable module prefix in {dotted!r}"
    raise ModuleNotFoundError(msg)


def _producer_source(producer: SignalLedgerProducer) -> str:
    obj = _resolve(producer.write_entry_point)
    fn = getattr(obj, "__func__", obj)
    return inspect.getsource(fn)


def orphaned_ledgers(
    *,
    registry: tuple[SignalLedgerProducer, ...],
    enumerated: dict[str, type[Model]],
) -> dict[str, str]:
    """Every read ledger lacking a LIVE writer, mapped to why — the capstone core.

    A ledger is orphaned when it has no registry entry, OR its producer's write
    entry point does not resolve, OR the write expression is absent from that
    producer's source (a stubbed/gutted writer — the fail-open regression class).
    Shared by the real assertion and the anti-vacuity self-test so the gate is
    proven to actually fire.
    """
    by_ledger = {producer.ledger: producer for producer in registry}
    orphans: dict[str, str] = {}
    for ledger in enumerated:
        producer = by_ledger.get(ledger)
        if producer is None:
            orphans[ledger] = "no registered producer (a signal reads a ledger nothing writes)"
            continue
        try:
            source = _producer_source(producer)
        except (ModuleNotFoundError, AttributeError, TypeError, OSError) as exc:
            orphans[ledger] = f"producer {producer.write_entry_point} does not resolve: {exc}"
            continue
        if producer.write_call not in source:
            orphans[ledger] = (
                f"producer {producer.write_entry_point} no longer writes the ledger "
                f"(missing {producer.write_call!r}) — stubbed/gutted writer"
            )
    return orphans


class TestSignalLedgerProducersCapstone:
    """Every ledger the signal layer reads must have a live, exercised writer."""

    def test_no_signal_reads_an_unwritten_ledger(self) -> None:
        orphans = orphaned_ledgers(
            registry=SIGNAL_LEDGER_PRODUCERS,
            enumerated=ledger_models_read_by_signal_queries(),
        )
        assert not orphans, f"signal-layer ledgers with no live writer: {orphans}"

    def test_no_producer_entry_is_a_phantom(self) -> None:
        # Reverse direction: every registry entry must name a ledger the queries
        # actually read — a producer for a no-longer-read ledger is dead surface.
        read = set(ledger_models_read_by_signal_queries())
        phantom = {producer.ledger for producer in SIGNAL_LEDGER_PRODUCERS} - read
        assert not phantom, f"registered producer(s) for ledgers the queries do not read: {sorted(phantom)}"

    def test_every_producer_has_an_exercised_integration_test(self) -> None:
        for producer in SIGNAL_LEDGER_PRODUCERS:
            path = _REPO_ROOT / producer.integration_test
            assert path.is_file(), f"{producer.ledger}: integration test {producer.integration_test} is missing"
            assert producer.ledger in path.read_text(encoding="utf-8"), (
                f"{producer.ledger}: integration test {producer.integration_test} never references the ledger"
            )

    def test_cardinality_floor_anti_vacuity(self) -> None:
        # A refactor that empties the enumeration must not turn the coverage lane
        # vacuously green; the six named ledgers plus MergeClear + TicketTransition.
        assert len(ledger_models_read_by_signal_queries()) >= 8
        assert len(SIGNAL_LEDGER_PRODUCERS) >= 8


class TestCapstoneFiresRedOnAStubbedProducer:
    """Anti-vacuity: the capstone must go RED when a producer is stubbed or missing.

    A conformance gate that can never fail is worthless. These drive
    ``orphaned_ledgers`` against synthetic registries so the two failure modes the
    capstone guards — a missing producer and a gutted writer — are proven to be
    named, not silently passed.
    """

    _MODELS: ClassVar[dict[str, type]] = {"RedMrFixAttempt": object}  # only the name is read by the detector

    def test_missing_producer_is_named(self) -> None:
        orphans = orphaned_ledgers(registry=(), enumerated=self._MODELS)
        assert "RedMrFixAttempt" in orphans
        assert "no registered producer" in orphans["RedMrFixAttempt"]

    def test_gutted_writer_is_named(self) -> None:
        # A real producer path whose source does NOT contain the declared write
        # call models the fail-open stub: the ledger is reported orphaned.
        stubbed = (
            SignalLedgerProducer(
                ledger="RedMrFixAttempt",
                write_entry_point="teatree.loop.dispatch_gates.claim_red_mr_fix",
                write_call="THIS_WRITE_WAS_REMOVED_BY_A_STUB",
                integration_test="tests/teatree_loop/test_sig2_red_mr_ledger.py",
            ),
        )
        orphans = orphaned_ledgers(registry=stubbed, enumerated=self._MODELS)
        assert "RedMrFixAttempt" in orphans
        assert "stubbed/gutted writer" in orphans["RedMrFixAttempt"]

    def test_healthy_registry_reports_no_orphans(self) -> None:
        # The positive control: the real registry against a real read ledger is clean.
        orphans = orphaned_ledgers(
            registry=SIGNAL_LEDGER_PRODUCERS,
            enumerated={"RedMrFixAttempt": ledger_models_read_by_signal_queries()["RedMrFixAttempt"]},
        )
        assert orphans == {}
