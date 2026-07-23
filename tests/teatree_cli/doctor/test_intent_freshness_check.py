"""``_check_intent_freshness`` — the `t3 doctor` "no owner-intent silently rots" gate.

The directive loop once sat masked for ~8 days while owner directives piled up at
``CAPTURED``, never interpreted, producing ZERO signal. This gate HARD-FAILs when a
consumable intent queue is non-empty while its consumer is not live, and WARNs when a
live consumer lets an item age past the freshness threshold. The pure
:func:`intent_freshness_findings` is exercised against an injected clock; the wrapper
runs against the real Loop table + directive/question queues.
"""

import io
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase

from teatree.cli.doctor.checks_intent import (
    INTENT_FRESHNESS_THRESHOLD,
    IntentItem,
    IntentQueue,
    _check_intent_freshness,
    intent_freshness_findings,
)
from teatree.core.factory.factory_signals import FactorySignalsReport, SignalVerdict
from teatree.core.models import DeferredQuestion, Directive, Loop
from teatree.loop.self_improve.budget import BudgetVerdict
from teatree.loops.directive_loop.guards import DirectiveLoopSettings
from teatree.loops.outer_loop.guards import MIN_CRITIC_SAMPLE, CriticLiveness, GuardSeams

_NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def _item(ref: str, *, age_hours: float) -> IntentItem:
    return IntentItem(ref=ref, created_at=_NOW - timedelta(hours=age_hours))


def _queue(*, live: bool, pending: tuple[IntentItem, ...]) -> IntentQueue:
    return IntentQueue(
        label="directive",
        consumer_loop="directive_loop",
        remediation="unmask it: t3 loop enable directive_loop --emergency",
        consumer_live=live,
        pending=pending,
    )


class TestIntentFreshnessFindings:
    """The pure red→green contract, driven by an injected clock."""

    def test_dead_consumer_with_pending_work_is_a_gating_fail_naming_items(self) -> None:
        queue = _queue(live=False, pending=(_item("directive #12", age_hours=200),))
        findings = intent_freshness_findings([queue], now=_NOW)
        assert len(findings) == 1
        assert findings[0].gating is True
        assert "FAIL" in findings[0].message
        assert "directive #12" in findings[0].message
        assert "t3 loop enable directive_loop --emergency" in findings[0].message

    def test_dead_consumer_finding_subsumes_staleness_one_finding_only(self) -> None:
        # A dead consumer with BOTH a stale and a fresh item still emits exactly one
        # gating finding — the dead-consumer bug short-circuits per-item staleness.
        queue = _queue(live=False, pending=(_item("directive #1", age_hours=200), _item("directive #2", age_hours=1)))
        findings = intent_freshness_findings([queue], now=_NOW)
        assert len(findings) == 1
        assert findings[0].gating is True
        assert "2 directive item(s)" in findings[0].message

    def test_live_consumer_with_stale_item_is_a_nongating_warn(self) -> None:
        queue = _queue(live=True, pending=(_item("directive #7", age_hours=30),))
        findings = intent_freshness_findings([queue], now=_NOW)
        assert len(findings) == 1
        assert findings[0].gating is False
        assert "WARN" in findings[0].message
        assert "directive #7" in findings[0].message
        assert "aging" in findings[0].message

    def test_live_consumer_with_fresh_item_yields_nothing(self) -> None:
        queue = _queue(live=True, pending=(_item("directive #7", age_hours=1),))
        assert intent_freshness_findings([queue], now=_NOW) == []

    def test_empty_queue_yields_nothing_even_when_consumer_is_dead(self) -> None:
        assert intent_freshness_findings([_queue(live=False, pending=())], now=_NOW) == []

    def test_threshold_boundary_is_inclusive(self) -> None:
        exact = _queue(live=True, pending=(_item("directive #9", age_hours=24),))
        assert len(intent_freshness_findings([exact], now=_NOW, threshold=INTENT_FRESHNESS_THRESHOLD)) == 1

    def test_message_is_stable_as_the_queue_ages(self) -> None:
        # The watchdog content-hashes the message into its idempotency key, so an age
        # that ticks hourly would re-DM the owner every hour the queue sits.
        queue = _queue(live=False, pending=(_item("directive #1", age_hours=200),))
        first = intent_freshness_findings([queue], now=_NOW)[0].message
        later = intent_freshness_findings([queue], now=_NOW + timedelta(hours=10))[0].message
        assert first == later

    def test_enumeration_is_bounded_with_an_and_n_more_tail(self) -> None:
        items = tuple(_item(f"directive #{n}", age_hours=200) for n in range(20))
        message = intent_freshness_findings([_queue(live=False, pending=items)], now=_NOW)[0].message
        assert "and 15 more" in message
        assert "directive #19" not in message


def _live_critic() -> CriticLiveness:
    return CriticLiveness(live=True, verdict_count=MIN_CRITIC_SAMPLE)


def _open_directive_consumer() -> tuple[DirectiveLoopSettings, GuardSeams]:
    """Settings + seams that make the directive guard chain allow, as its tick would."""
    settings = SimpleNamespace(directive_loop_enabled=True, factory_score_enabled=True, directive_verify_days=7)
    report = FactorySignalsReport(
        window_days=28, generated_at=datetime(2026, 1, 1, tzinfo=UTC), signals=[], verdict=SignalVerdict.OK
    )
    seams = GuardSeams(critic_probe=_live_critic, signal_report=report, budget=BudgetVerdict.allow())
    return settings, seams


def _run(*, open_directive_consumer: bool = False) -> tuple[bool, str]:
    settings, seams = _open_directive_consumer() if open_directive_consumer else (None, None)
    buf = io.StringIO()
    with redirect_stdout(buf):
        ok = _check_intent_freshness(settings=settings, seams=seams)
    return ok, buf.getvalue()


def _backdate_directive(directive: Directive, *, hours: float) -> None:
    from django.utils import timezone  # noqa: PLC0415 — test-local clock read

    Directive.objects.filter(pk=directive.pk).update(created_at=timezone.now() - timedelta(hours=hours))


class TestCheckIntentFreshness(TestCase):
    """The ORM-reading wrapper against the real Loop table (seeded default loops)."""

    def test_empty_queues_pass_silently(self) -> None:
        ok, out = _run()
        assert ok is True
        assert out == ""

    def test_stuck_directive_with_masked_loop_fails_and_names_it(self) -> None:
        # `directive_loop` ships disabled (masked) by default — the exact incident.
        directive = Directive.objects.capture("cap 1 PR per repo", source=Directive.Source.CLI)
        _backdate_directive(directive, hours=200)
        ok, out = _run()
        assert ok is False
        assert "FAIL" in out
        assert f"directive #{directive.pk}" in out
        assert "directive_loop" in out

    def test_directive_guard_refusal_is_unconsumed_even_with_an_unmasked_loop(self) -> None:
        # Unmasking the loop is NOT enough: the fail-closed guard chain (the DARK
        # `directive_loop_enabled` flag first) still refuses every tick, so the queue
        # has no live consumer and the remediation must name the flag, not the mask.
        Loop.objects.filter(name="directive_loop").update(enabled=True)
        directive = Directive.objects.capture("cap 1 PR per repo", source=Directive.Source.CLI)
        ok, out = _run()
        assert ok is False
        assert "FAIL" in out
        assert f"directive #{directive.pk}" in out
        assert "directive_loop_enabled" in out

    def test_stuck_directive_with_live_consumer_warns_but_does_not_gate(self) -> None:
        Loop.objects.filter(name="directive_loop").update(enabled=True)
        directive = Directive.objects.capture("cap 1 PR per repo", source=Directive.Source.CLI)
        _backdate_directive(directive, hours=30)
        ok, out = _run(open_directive_consumer=True)
        assert ok is True
        assert "WARN" in out
        assert f"directive #{directive.pk}" in out

    def test_fresh_directive_with_live_consumer_is_silent(self) -> None:
        Loop.objects.filter(name="directive_loop").update(enabled=True)
        Directive.objects.capture("cap 1 PR per repo", source=Directive.Source.CLI)
        ok, out = _run(open_directive_consumer=True)
        assert ok is True
        assert out == ""

    def test_unmirrored_owner_question_with_masked_dispatch_loop_fails(self) -> None:
        Loop.objects.filter(name="dispatch").update(enabled=False)
        question = DeferredQuestion.record("Which target branch — main or develop?", session_id="s1")
        assert question.slack_ts == ""
        ok, out = _run()
        assert ok is False
        assert "FAIL" in out
        assert f"question #{question.pk}" in out
        assert "dispatch" in out

    def test_delivered_owner_question_with_masked_dispatch_loop_does_not_gate(self) -> None:
        # A MIRRORED question has been fully drained by the dispatch loop's poster —
        # it stays `pending` only until the HUMAN answers, so a masked dispatch loop
        # (every away-mode preset masks it) must not red the box on it.
        Loop.objects.filter(name="dispatch").update(enabled=False)
        question = DeferredQuestion.record("Which target branch — main or develop?", session_id="s1")
        question.mark_mirrored(channel="C1", slack_ts="1700000000.1")
        ok, out = _run()
        assert ok is True
        assert out == ""

    def test_clarifying_directive_awaiting_the_human_does_not_gate(self) -> None:
        # A CLARIFYING directive whose clarify questions are UNANSWERED is the OWNER's
        # work, not the directive loop's — `_advance_clarifying` returns `waiting`
        # (`awaiting_clarification`). Structurally identical to a DELIVERED-but-unanswered
        # question, so a masked directive_loop must not red the box on it.
        directive = Directive.objects.capture("ambiguous", source=Directive.Source.CLI)
        directive.mark_clarifying()
        clarify = DeferredQuestion.record("which?", options_hash=f"directive_clarify:{directive.pk}:0:0")
        clarify.mark_mirrored(channel="C1", slack_ts="1700000000.1")
        _backdate_directive(directive, hours=200)
        ok, out = _run()
        assert ok is True
        assert out == ""

    def test_answered_clarifying_directive_is_still_consumer_work(self) -> None:
        # Every clarify question answered but not yet re-interpreted IS the directive
        # loop's work (`_advance_clarifying` re-dispatches), so a masked loop gates.
        directive = Directive.objects.capture("ambiguous", source=Directive.Source.CLI)
        directive.mark_clarifying()
        clarify = DeferredQuestion.record("which?", options_hash=f"directive_clarify:{directive.pk}:0:0")
        DeferredQuestion.consume(clarify.pk, answer="this one")
        ok, out = _run()
        assert ok is False
        assert "FAIL" in out
        assert f"directive #{directive.pk}" in out

    def test_a_crashing_read_degrades_to_ok_without_reddening_the_run(self) -> None:
        with mock.patch("teatree.loops.preset_status.effective_verdicts", side_effect=RuntimeError("no such table")):
            ok, out = _run()
        assert ok is True
        assert "WARN" in out
        assert "crashed" in out

    def test_internal_audience_question_is_ignored(self) -> None:
        # Only OWNER_QUESTION rows are owner intent; INTERNAL (self-health) rows never
        # reach the owner and must not gate the check.
        Loop.objects.filter(name="dispatch").update(enabled=False)
        DeferredQuestion.record("repair stalled", session_id="s1", audience=DeferredQuestion.Audience.INTERNAL)
        ok, out = _run()
        assert ok is True
        assert out == ""
