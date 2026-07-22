"""``_check_intent_freshness`` — the `t3 doctor` "no owner-intent silently rots" gate.

The directive loop once sat masked for ~8 days while owner directives piled up at
``CAPTURED``, never interpreted, producing ZERO signal. This gate HARD-FAILs when a
consumable intent queue is non-empty while its consuming loop is not admitting, and
WARNs when a live consumer lets an item age past the freshness threshold. The pure
:func:`intent_freshness_findings` is exercised against an injected clock; the wrapper
runs against the real Loop table + directive/question queues.
"""

import io
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta

from django.test import TestCase

from teatree.cli.doctor.checks_intent import (
    INTENT_FRESHNESS_THRESHOLD,
    IntentItem,
    IntentQueue,
    _check_intent_freshness,
    intent_freshness_findings,
)
from teatree.core.models import DeferredQuestion, Directive, Loop

_NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def _item(ref: str, *, age_hours: float) -> IntentItem:
    return IntentItem(ref=ref, created_at=_NOW - timedelta(hours=age_hours))


def _queue(*, admits: bool, pending: tuple[IntentItem, ...]) -> IntentQueue:
    return IntentQueue(
        label="directive",
        consumer_loop="directive_loop",
        remediation="t3 loop enable directive_loop --emergency",
        loop_admits=admits,
        pending=pending,
    )


class TestIntentFreshnessFindings:
    """The pure red→green contract, driven by an injected clock."""

    def test_masked_loop_with_pending_work_is_a_gating_fail_naming_items(self) -> None:
        queue = _queue(admits=False, pending=(_item("directive #12", age_hours=200),))
        findings = intent_freshness_findings([queue], now=_NOW)
        assert len(findings) == 1
        assert findings[0].gating is True
        assert "FAIL" in findings[0].message
        assert "directive #12" in findings[0].message
        assert "not admitting" in findings[0].message
        assert "t3 loop enable directive_loop --emergency" in findings[0].message

    def test_masked_finding_subsumes_staleness_one_finding_only(self) -> None:
        # A masked loop with BOTH a stale and a fresh item still emits exactly one
        # gating finding — the masked-consumer bug short-circuits per-item staleness.
        queue = _queue(admits=False, pending=(_item("directive #1", age_hours=200), _item("directive #2", age_hours=1)))
        findings = intent_freshness_findings([queue], now=_NOW)
        assert len(findings) == 1
        assert findings[0].gating is True
        assert "2 directive item(s)" in findings[0].message

    def test_live_consumer_with_stale_item_is_a_nongating_warn(self) -> None:
        queue = _queue(admits=True, pending=(_item("directive #7", age_hours=30),))
        findings = intent_freshness_findings([queue], now=_NOW)
        assert len(findings) == 1
        assert findings[0].gating is False
        assert "WARN" in findings[0].message
        assert "directive #7" in findings[0].message
        assert "aging" in findings[0].message

    def test_live_consumer_with_fresh_item_yields_nothing(self) -> None:
        queue = _queue(admits=True, pending=(_item("directive #7", age_hours=1),))
        assert intent_freshness_findings([queue], now=_NOW) == []

    def test_empty_queue_yields_nothing_even_when_masked(self) -> None:
        assert intent_freshness_findings([_queue(admits=False, pending=())], now=_NOW) == []

    def test_threshold_boundary_is_inclusive(self) -> None:
        exact = _queue(admits=True, pending=(_item("directive #9", age_hours=24),))
        assert len(intent_freshness_findings([exact], now=_NOW, threshold=INTENT_FRESHNESS_THRESHOLD)) == 1


def _run() -> tuple[bool, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        ok = _check_intent_freshness()
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

    def test_stuck_directive_with_live_loop_warns_but_does_not_gate(self) -> None:
        Loop.objects.filter(name="directive_loop").update(enabled=True)
        directive = Directive.objects.capture("cap 1 PR per repo", source=Directive.Source.CLI)
        _backdate_directive(directive, hours=30)
        ok, out = _run()
        assert ok is True
        assert "WARN" in out
        assert f"directive #{directive.pk}" in out

    def test_fresh_directive_with_live_loop_is_silent(self) -> None:
        Loop.objects.filter(name="directive_loop").update(enabled=True)
        Directive.objects.capture("cap 1 PR per repo", source=Directive.Source.CLI)
        ok, out = _run()
        assert ok is True
        assert out == ""

    def test_owner_question_with_masked_dispatch_loop_fails(self) -> None:
        Loop.objects.filter(name="dispatch").update(enabled=False)
        question = DeferredQuestion.record("Which target branch — main or develop?", session_id="s1")
        ok, out = _run()
        assert ok is False
        assert "FAIL" in out
        assert f"question #{question.pk}" in out
        assert "dispatch" in out

    def test_internal_audience_question_is_ignored(self) -> None:
        # Only OWNER_QUESTION rows are owner intent; INTERNAL (self-health) rows never
        # reach the owner and must not gate the check.
        Loop.objects.filter(name="dispatch").update(enabled=False)
        DeferredQuestion.record("repair stalled", session_id="s1", audience=DeferredQuestion.Audience.INTERNAL)
        ok, out = _run()
        assert ok is True
        assert out == ""
