"""The tick fold that turns per-PR sweep signals into ONE set-level claim (#4090).

Exercises the whole fold end to end — real :class:`ScanSignal` payloads in, a
fake ``main`` probe and a recording notifier as the only seams — so the signal
decoding, the per-repo grouping, the once-per-claim announcement and the
degrade-to-a-quiet-tick contract are all covered by the path the loop runs.
"""

import json
import subprocess

import pytest

from teatree.loop.red_set_report import SetVerdict
from teatree.loop.red_set_surface import _default_main_checks, record_red_set
from teatree.loop.scanners.base import ScanSignal

SLUG = "souliane/teatree"
_FORGE_DOWN = "forge down"
_SLACK_DOWN = "slack down"


class RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, *, text: str, idempotency_key: str) -> None:
        self.calls.append((text, idempotency_key))


def _skip_signal(pr_id: int, *failing: str, base_current: bool = True, overlay: str = "t3-teatree") -> ScanSignal:
    return ScanSignal(
        kind="pr_sweep.skip",
        summary=f"{SLUG}#{pr_id} skip (ci_red)",
        payload={
            "slug": SLUG,
            "pr_id": pr_id,
            "decision": "skip",
            "reason": "ci_red",
            "merged": False,
            "overlay": overlay,
            "url": f"https://github.com/{SLUG}/pull/{pr_id}",
            "failing_required": list(failing),
            "base_current": base_current,
        },
    )


def _green_main(*, slug: str, overlay: str) -> frozenset[str]:
    return frozenset()


def _unreadable_main(*, slug: str, overlay: str) -> None:
    return None


class TestFold:
    def test_a_mutually_blocking_red_set_is_reported_and_announced(self) -> None:
        notifier = RecordingNotifier()

        reports = record_red_set(
            [_skip_signal(4101, "shard-a"), _skip_signal(4102, "shard-b")],
            main_checks=_green_main,
            notify=notifier,
        )

        assert [report.verdict for report in reports] == [SetVerdict.POSSIBLE_CYCLE]
        assert len(notifier.calls) == 1
        text, key = notifier.calls[0]
        assert "possible-cycle" in text
        assert key == f"pr_sweep_red_set:{reports[0].signature()}"

    def test_the_same_stalled_set_keys_the_same_announcement_every_tick(self) -> None:
        # The ledger no-ops a key it has already delivered, so an unchanged set is
        # announced ONCE however long it stays stalled.
        notifier = RecordingNotifier()
        signals = [_skip_signal(4101, "shard-a"), _skip_signal(4102, "shard-b")]

        record_red_set(signals, main_checks=_green_main, notify=notifier)
        record_red_set(signals, main_checks=_green_main, notify=notifier)

        assert len({key for _text, key in notifier.calls}) == 1

    def test_only_a_possible_cycle_is_announced(self) -> None:
        notifier = RecordingNotifier()

        reports = record_red_set(
            [_skip_signal(4101, "shard-a"), _skip_signal(4102, "shard-a")],
            main_checks=_green_main,
            notify=notifier,
        )

        assert [report.verdict for report in reports] == [SetVerdict.SHARED_CAUSE]
        assert notifier.calls == []

    def test_an_unreadable_main_never_claims_a_cycle(self) -> None:
        notifier = RecordingNotifier()

        reports = record_red_set(
            [_skip_signal(4101, "shard-a"), _skip_signal(4102, "shard-b")],
            main_checks=_unreadable_main,
            notify=notifier,
        )

        assert [report.verdict for report in reports] == [SetVerdict.MAIN_INDETERMINATE]
        assert notifier.calls == []

    def test_each_repo_gets_its_own_set(self) -> None:
        other = ScanSignal(
            kind="pr_sweep.skip",
            summary="souliane/other#7 skip (ci_red)",
            payload={
                "slug": "souliane/other",
                "pr_id": 7,
                "overlay": "t3-teatree",
                "url": "",
                "failing_required": ["shard-z"],
                "base_current": True,
            },
        )
        notifier = RecordingNotifier()

        reports = record_red_set(
            [_skip_signal(4101, "shard-a"), _skip_signal(4102, "shard-b"), other],
            main_checks=_green_main,
            notify=notifier,
        )

        assert {report.slug for report in reports} == {SLUG, "souliane/other"}
        assert {report.verdict for report in reports} == {SetVerdict.POSSIBLE_CYCLE, SetVerdict.INDEPENDENT}

    def test_the_same_repo_under_two_overlays_stays_two_sets(self) -> None:
        notifier = RecordingNotifier()

        reports = record_red_set(
            [_skip_signal(4101, "shard-a"), _skip_signal(4102, "shard-b", overlay="other-overlay")],
            main_checks=_green_main,
            notify=notifier,
        )

        assert [report.verdict for report in reports] == [SetVerdict.INDEPENDENT, SetVerdict.INDEPENDENT]
        assert notifier.calls == []


class TestQuietPaths:
    def test_a_tick_with_no_sweep_signals_probes_nothing(self) -> None:
        probed: list[str] = []

        def probe(*, slug: str, overlay: str) -> frozenset[str]:
            probed.append(slug)
            return frozenset()

        assert record_red_set([ScanSignal(kind="my_pr.failed", summary="x")], main_checks=probe) == []
        assert probed == []

    def test_a_green_pr_is_not_part_of_the_red_set(self) -> None:
        assert record_red_set([_skip_signal(4101)], main_checks=_green_main) == []

    def test_a_malformed_payload_is_dropped_not_raised(self) -> None:
        malformed = ScanSignal(kind="pr_sweep.skip", summary="x", payload={"slug": SLUG, "pr_id": "not-an-int"})

        assert record_red_set([malformed], main_checks=_green_main) == []

    def test_a_raising_probe_is_indeterminate_never_a_green_main(self) -> None:
        # A probe that blows up is the same condition as one that cannot tell —
        # degrading to "main is green" would claim a cycle for an inherited red.
        notifier = RecordingNotifier()

        def boom(*, slug: str, overlay: str) -> frozenset[str]:
            raise RuntimeError(_FORGE_DOWN)

        reports = record_red_set(
            [_skip_signal(4101, "shard-a"), _skip_signal(4102, "shard-b")],
            main_checks=boom,
            notify=notifier,
        )

        assert [report.verdict for report in reports] == [SetVerdict.MAIN_INDETERMINATE]
        assert notifier.calls == []

    def test_a_failing_notifier_never_aborts_the_tick(self) -> None:
        def boom(*, text: str, idempotency_key: str) -> None:
            raise RuntimeError(_SLACK_DOWN)

        reports = record_red_set(
            [_skip_signal(4101, "shard-a"), _skip_signal(4102, "shard-b")],
            main_checks=_green_main,
            notify=boom,
        )

        assert [report.verdict for report in reports] == [SetVerdict.POSSIBLE_CYCLE]


class TestMainProbe:
    """The one live read the report makes — ``main``'s own check-runs.

    Only the subprocess is faked; the argv the probe builds, the JSON decoding and
    the green/failing classification all run for real.
    """

    @staticmethod
    def _probe(monkeypatch: pytest.MonkeyPatch, *, stdout: str, returncode: int = 0) -> frozenset[str] | None:
        captured: list[list[str]] = []

        def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            captured.append(argv)
            return subprocess.CompletedProcess(argv, returncode, stdout, "")

        monkeypatch.setattr("teatree.loop.red_set_surface.run_allowed_to_fail", fake_run)
        monkeypatch.setattr("teatree.loop.red_set_surface._github_token", lambda _overlay: "")
        result = _default_main_checks(slug=SLUG, overlay="t3-teatree")
        assert f"repos/{SLUG}/commits/main/check-runs" in captured[0]
        return result

    def test_a_completed_non_green_run_is_a_failing_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = json.dumps(
            [
                {"name": "test (3.13)", "status": "completed", "conclusion": "failure"},
                {"name": "uv-audit", "status": "completed", "conclusion": "success"},
                {"name": "eval", "status": "completed", "conclusion": "skipped"},
            ]
        )

        assert self._probe(monkeypatch, stdout=payload) == frozenset({"test (3.13)"})

    def test_a_still_running_check_is_not_a_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = json.dumps([{"name": "test (3.13)", "status": "in_progress", "conclusion": None}])

        assert self._probe(monkeypatch, stdout=payload) == frozenset()

    def test_no_check_runs_at_all_is_indeterminate_not_green(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Nothing has reported on that commit, so there is no evidence main is green.
        assert self._probe(monkeypatch, stdout="[]") is None

    def test_a_non_zero_exit_is_indeterminate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._probe(monkeypatch, stdout="", returncode=1) is None

    def test_an_unparseable_body_is_indeterminate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._probe(monkeypatch, stdout="<html>rate limited</html>") is None
