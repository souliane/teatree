from contextvars import ContextVar
from threading import Event
from typing import cast
from unittest.mock import patch

from django.test import TestCase

import teatree.loop.overlay_followup_jobs as overlay_followup_jobs_module
from teatree.backends.messaging_noop import NoopMessagingBackend
from teatree.contrib.t3_teatree.overlay import TeatreeOverlay
from teatree.core.backend_factory import OverlayBackends
from teatree.core.models import Loop, Mode, ModeOverride
from teatree.core.overlay import OverlayReview
from teatree.loop.job_identity import _ScannerJob
from teatree.loop.phases.scan import scan_phase
from teatree.loop.scanners.base import Scanner, ScanSignal
from teatree.loops.followup.loop import MINI_LOOP


class _ExtraScanner:
    name = "overlay_extra"

    def scan(self) -> list[ScanSignal]:
        return [ScanSignal(kind="overlay.extra", summary="ran")]


class _BadScanScanner:
    name = "bad_scan"
    scan = 5


class _ReservedScanner:
    name = "pr_sweep"

    def scan(self) -> list[ScanSignal]:
        return []


class _BuiltInNameScanner:
    name = "review_nag"

    def scan(self) -> list[ScanSignal]:
        return []


class _UnnamedScanner:
    name = ""

    def scan(self) -> list[ScanSignal]:
        return []


class _HookReview(OverlayReview):
    def __init__(self, result: object) -> None:
        self.result = result
        self.called_with: list[OverlayBackends] = []

    def get_followup_scanners(self, backend: OverlayBackends) -> list[Scanner]:
        self.called_with.append(backend)
        if isinstance(self.result, Exception):
            raise self.result
        return cast("list[Scanner]", self.result)


class TestOverlayFollowupScanners(TestCase):
    def setUp(self) -> None:
        loops = list(Loop.objects.values_list("name", flat=True))
        for name, egress in (("afk", "forbid"), ("present", "allow")):
            Mode.objects.update_or_create(name=name, defaults={"entries": dict.fromkeys(loops, True), "egress": egress})
        ModeOverride.objects.set_override("present", reason="test posture")

    def _backend(self, name: str, review: _HookReview | None = None) -> OverlayBackends:
        overlay = TeatreeOverlay()
        if review is not None:
            overlay.review = review
        return OverlayBackends(name=name, overlay=overlay, messaging=NoopMessagingBackend())

    def test_the_overlay_scanner_runs_in_the_followup_loop_tagged_with_its_overlay(self) -> None:
        scanner = _ExtraScanner()
        review = _HookReview([scanner])
        backend = self._backend("alpha", review)

        jobs = MINI_LOOP.build_jobs(backends=[backend])
        extra = [job for job in jobs if job.scanner is scanner]
        assert len(extra) == 1
        assert extra[0].overlay == "alpha"
        assert review.called_with == [backend]
        assert "review_nag" in {job.scanner.name for job in jobs}

        outcome = scan_phase(extra)
        assert outcome.errors == {}
        assert outcome.signals == [ScanSignal(kind="overlay.extra", summary="ran", payload={"overlay": "alpha"})]

    def test_a_posture_that_forbids_egress_never_asks_the_overlay(self) -> None:
        review = _HookReview([_ExtraScanner()])
        backend = self._backend("alpha", review)
        ModeOverride.objects.set_override("afk", reason="test posture")
        assert MINI_LOOP.build_jobs(backends=[backend]) == []
        assert review.called_with == []

        ModeOverride.objects.set_override("present", reason="test posture")
        assert "overlay_extra" in {job.scanner.name for job in MINI_LOOP.build_jobs(backends=[backend])}

    def test_an_overlay_without_a_class_keeps_only_the_built_in_scanners(self) -> None:
        backend = OverlayBackends(name="alpha", overlay=None, messaging=NoopMessagingBackend())
        assert {job.scanner.name for job in MINI_LOOP.build_jobs(backends=[backend])} == {
            "review_nag",
            "review_request_merge_react",
            "review_request_resume",
        }
        assert TeatreeOverlay().review.get_followup_scanners(backend) == []

    def test_a_raising_hook_is_isolated_to_its_overlay(self) -> None:
        raising = self._backend("alpha", _HookReview(ValueError("bad hook")))
        healthy_scanner = _ExtraScanner()
        healthy = self._backend("beta", _HookReview([healthy_scanner]))

        jobs = MINI_LOOP.build_jobs(backends=[raising, healthy])
        for tag in ("alpha", "beta"):
            assert "review_nag" in {job.scanner.name for job in jobs if job.overlay == tag}
        assert any(job.scanner is healthy_scanner and job.overlay == "beta" for job in jobs)

        failures = [job for job in jobs if job.scanner.name == "overlay_followup_scanners"]
        assert len(failures) == 1
        outcome = scan_phase(failures)
        assert set(outcome.errors) == {"overlay_followup_scanners[alpha]"}
        assert outcome.errors["overlay_followup_scanners[alpha]"] == "ValueError: bad hook"

    def test_blocking_hook_is_bounded_and_reported(self) -> None:
        release = Event()

        class BlockingReview(OverlayReview):
            def get_followup_scanners(self, backend: OverlayBackends) -> list[Scanner]:
                release.wait()
                return []

        try:
            with patch.object(overlay_followup_jobs_module, "HOOK_DEADLINE_SECONDS", 0.01):
                jobs = MINI_LOOP.build_jobs(backends=[self._backend("alpha", BlockingReview())])
            refused = [job for job in jobs if job.scanner.name == "overlay_followup_scanners"]
            assert len(refused) == 1
            assert scan_phase(refused).errors == {"overlay_followup_scanners[alpha]": "hook timed out after 0.01s"}
        finally:
            release.set()

    def test_a_stuck_hook_does_not_start_another_thread_on_the_next_tick(self) -> None:
        release = Event()
        calls: list[str] = []

        class BlockingReview(OverlayReview):
            def get_followup_scanners(self, backend: OverlayBackends) -> list[Scanner]:
                calls.append(backend.name)
                release.wait()
                return []

        try:
            backend = self._backend("stuck", BlockingReview())
            with patch.object(overlay_followup_jobs_module, "HOOK_DEADLINE_SECONDS", 0.01):
                first = MINI_LOOP.build_jobs(backends=[backend])
                second = MINI_LOOP.build_jobs(backends=[backend])
            assert len([job for job in first if job.scanner.name == "overlay_followup_scanners"]) == 1
            assert len([job for job in second if job.scanner.name == "overlay_followup_scanners"]) == 1
            assert calls == ["stuck"]
        finally:
            release.set()

    def test_hook_receives_selection_context(self) -> None:
        selection = ContextVar("followup_test_selection", default="missing")
        seen: list[str] = []

        class ContextReview(OverlayReview):
            def get_followup_scanners(self, backend: OverlayBackends) -> list[Scanner]:
                seen.append(selection.get())
                return []

        token = selection.set("current-tick")
        try:
            MINI_LOOP.build_jobs(backends=[self._backend("context", ContextReview())])
        finally:
            selection.reset(token)
        assert seen == ["current-tick"]

    def test_overlay_scanner_error_cannot_overwrite_a_builtin_error(self) -> None:
        class FailingScanner:
            name = "slack_mentions"

            def scan(self) -> list[ScanSignal]:
                msg = "scanner failed"
                raise ValueError(msg)

        backend = self._backend("collision", _HookReview([FailingScanner()]))
        jobs = [
            _ScannerJob(scanner=FailingScanner(), overlay="collision"),
            *overlay_followup_jobs_module.overlay_followup_jobs(backend),
        ]
        outcome = scan_phase(jobs)
        assert len(outcome.errors) == 2
        assert "slack_mentions[collision]" in outcome.errors
        assert any(label != "slack_mentions[collision]" and "slack_mentions" in label for label in outcome.errors)

    def test_a_hook_returning_a_non_scanner_is_refused(self) -> None:
        cases = (
            ([object()], "object"),
            (None, "NoneType"),
            ([_BadScanScanner()], "_BadScanScanner"),
            ([_ExtraScanner], "type"),
        )
        for result, bad_type in cases:
            with self.subTest(result=result):
                backend = self._backend("alpha", _HookReview(result))
                jobs = MINI_LOOP.build_jobs(backends=[backend])
                assert "review_nag" in {job.scanner.name for job in jobs}
                failures = [job for job in jobs if job.scanner.name == "overlay_followup_scanners"]
                assert len(failures) == 1
                outcome = scan_phase(failures)
                assert set(outcome.errors) == {"overlay_followup_scanners[alpha]"}
                assert bad_type in outcome.errors["overlay_followup_scanners[alpha]"]

    def test_duplicate_and_reserved_names_are_refused_without_losing_other_jobs(self) -> None:
        review = _HookReview([_ExtraScanner(), _ExtraScanner(), _ReservedScanner(), _UnnamedScanner()])
        jobs = MINI_LOOP.build_jobs(backends=[self._backend("alpha", review)])

        assert [job.scanner.name for job in jobs].count("overlay_extra") == 1
        assert not any(job.scanner.name == "pr_sweep" for job in jobs)
        outcome = scan_phase([job for job in jobs if job.scanner.name == "overlay_followup_scanners"])
        assert "duplicate" in outcome.errors["overlay_followup_scanners[alpha]"]
        assert "pr_sweep" in outcome.errors["overlay_followup_scanners[alpha]"]
        assert "Invalid scanner name" in outcome.errors["overlay_followup_scanners[alpha]"]

    def test_builtin_followup_scanner_name_is_reserved(self) -> None:
        jobs = MINI_LOOP.build_jobs(backends=[self._backend("alpha", _HookReview([_BuiltInNameScanner()]))])
        assert not any(job.overlay_hook and job.scanner.name == "review_nag" for job in jobs)
        refused = [job for job in jobs if job.scanner.name == "overlay_followup_scanners"]
        assert scan_phase(refused).errors == {
            "overlay_followup_scanners[alpha]": "duplicate or reserved scanner name: review_nag"
        }

    def test_agent_routed_signal_is_refused(self) -> None:
        class AgentScanner:
            name = "agent_scanner"

            def scan(self) -> list[ScanSignal]:
                return [ScanSignal(kind="self_pr_review.dispatch", summary="agent")]

        jobs = MINI_LOOP.build_jobs(backends=[self._backend("alpha", _HookReview([AgentScanner()]))])
        outcome = scan_phase([job for job in jobs if job.scanner.name == "agent_scanner"])
        assert outcome.signals == []
        assert "agent-routed" in outcome.errors["agent_scanner[alpha][hook]"]

    def test_two_overlays_keep_their_extra_scanners_separate(self) -> None:
        alpha_scanner, beta_scanner = _ExtraScanner(), _ExtraScanner()
        alpha = self._backend("alpha", _HookReview([alpha_scanner]))
        beta = self._backend("beta", _HookReview([beta_scanner]))
        jobs = MINI_LOOP.build_jobs(backends=[alpha, beta])

        assert [
            (job.scanner, job.overlay) for job in jobs if job.scanner is alpha_scanner or job.scanner is beta_scanner
        ] == [
            (alpha_scanner, "alpha"),
            (beta_scanner, "beta"),
        ]
