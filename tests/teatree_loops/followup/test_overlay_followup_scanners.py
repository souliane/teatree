from typing import cast

from django.test import TestCase

from teatree.backends.messaging_noop import NoopMessagingBackend
from teatree.contrib.t3_teatree.overlay import TeatreeOverlay
from teatree.core.backend_factory import OverlayBackends
from teatree.core.models import Loop, Mode, ModeOverride
from teatree.core.overlay import OverlayReview
from teatree.loop.phases.scan import scan_phase
from teatree.loop.scanners.base import Scanner, ScanSignal
from teatree.loops.followup.loop import MINI_LOOP


class _ExtraScanner:
    name = "overlay_extra"

    def scan(self) -> list[ScanSignal]:
        return [ScanSignal(kind="overlay.extra", summary="ran")]


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
        assert "bad hook" in outcome.errors["overlay_followup_scanners[alpha]"]

    def test_a_hook_returning_a_non_scanner_is_refused(self) -> None:
        for result, bad_type in (([object()], "object"), (None, "NoneType")):
            with self.subTest(result=result):
                backend = self._backend("alpha", _HookReview(result))
                jobs = MINI_LOOP.build_jobs(backends=[backend])
                assert "review_nag" in {job.scanner.name for job in jobs}
                failures = [job for job in jobs if job.scanner.name == "overlay_followup_scanners"]
                assert len(failures) == 1
                outcome = scan_phase(failures)
                assert set(outcome.errors) == {"overlay_followup_scanners[alpha]"}
                assert bad_type in outcome.errors["overlay_followup_scanners[alpha]"]

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
