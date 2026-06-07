"""Dream QA-probe corpus tests (#1933).

A probe is idempotent on ``probe_key`` (sha256 of the question text), and
``record_result`` accumulates run/pass counts while recomputing the running
pass rate. The manager separates the prior-session retention corpus from the
full current corpus per overlay.
"""

import hashlib

import pytest
from django.db import IntegrityError
from django.test import TestCase

from teatree.core.models import DreamQaProbe


def _probe(
    question: str = "What gate runs before push?",
    *,
    overlay: str = "acme",
    prior: bool = False,
) -> DreamQaProbe:
    return DreamQaProbe.objects.create(
        probe_key=hashlib.sha256(question.encode("utf-8")).hexdigest(),
        question=question,
        expected_answer="the privacy + lint gate",
        source_memory_path="MEMORY.md",
        overlay=overlay,
        is_prior_session=prior,
    )


class TestProbeKey(TestCase):
    def test_probe_key_is_unique(self) -> None:
        _probe("Q1")
        duplicate_key = hashlib.sha256(b"Q1").hexdigest()
        with pytest.raises(IntegrityError):
            DreamQaProbe.objects.create(
                probe_key=duplicate_key,
                question="Q1 reworded but same key",
                expected_answer="x",
            )


class TestRecordResult(TestCase):
    def test_first_pass_sets_full_rate(self) -> None:
        probe = _probe()

        probe.record_result(passed=True)

        probe.refresh_from_db()
        assert probe.run_count == 1
        assert probe.pass_count == 1
        assert probe.last_pass_rate == pytest.approx(1.0)

    def test_first_fail_sets_zero_rate(self) -> None:
        probe = _probe()

        probe.record_result(passed=False)

        probe.refresh_from_db()
        assert probe.run_count == 1
        assert probe.pass_count == 0
        assert probe.last_pass_rate == pytest.approx(0.0)

    def test_rate_is_pass_over_run(self) -> None:
        probe = _probe()

        probe.record_result(passed=True)
        probe.record_result(passed=False)
        probe.record_result(passed=True)
        probe.record_result(passed=True)

        probe.refresh_from_db()
        assert probe.run_count == 4
        assert probe.pass_count == 3
        assert probe.last_pass_rate == pytest.approx(0.75)


class TestManager(TestCase):
    def test_prior_session_probes_filters_overlay_and_flag(self) -> None:
        prior = _probe("prior", overlay="acme", prior=True)
        _probe("current", overlay="acme", prior=False)
        _probe("other-overlay", overlay="widgets", prior=True)

        result = list(DreamQaProbe.objects.prior_session_probes("acme"))

        assert result == [prior]

    def test_current_corpus_returns_all_overlay_probes(self) -> None:
        _probe("a", overlay="acme")
        _probe("b", overlay="acme", prior=True)
        _probe("c", overlay="widgets")

        assert DreamQaProbe.objects.current_corpus("acme").count() == 2
