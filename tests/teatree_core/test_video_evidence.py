"""Deterministic leading-dead-frame check for test-plan video evidence.

The recurrence this pins: an E2E test-plan VIDEO with ~40s of blank/static
pre-roll (out of ~70s) and an unclear final frame was posted to a customer
ticket and neither the author nor the e2e-review gate caught it — the post path
machine-enforces image quality (red-box / dedup) but had ZERO video check.

:mod:`teatree.core.evidence.video_evidence` is the deterministic substitute, mirroring
:mod:`teatree.core.evidence.test_plan_validation`: it shells ffprobe/ffmpeg to measure
the LEADING static/blank run from t=0 and refuses a video whose dead lead
exceeds the configured budget. These tests generate real videos with ffmpeg —
one with a long blank lead (must FAIL), one tight (must PASS) — and assert the
ffmpeg-missing path degrades to a clear skip rather than crashing the post path.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from teatree.core.evidence import video_evidence as _ve
from teatree.core.evidence.video_evidence import (
    DEFAULT_MAX_DEAD_LEAD_SECONDS,
    VideoEvidenceError,
    VideoEvidenceReport,
    check_video_evidence,
)
from teatree.utils.run import CommandFailedError

_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
_needs_ffmpeg = pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")


def _ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    assert path is not None
    return path


def _write_video(path: Path, *, source: str, duration: float, fps: int = 10) -> Path:
    """Render a synthetic video from a lavfi *source* to *path*.

    A bare source (``testsrc``) takes its options after ``=``; a parameterised
    source (``color=c=black``) already carries an ``=``, so its size/rate/duration
    options join with ``:``.
    """
    joiner = ":" if "=" in source else "="
    lavfi_input = f"{source}{joiner}size=160x120:rate={fps}:duration={duration}"
    subprocess.run(
        [_ffmpeg(), "-y", "-f", "lavfi", "-i", lavfi_input, "-pix_fmt", "yuv420p", str(path)],
        check=True,
        capture_output=True,
    )
    return path


def _blank_lead_then_motion(path: Path, *, lead: float, motion: float) -> Path:
    """A video that is a solid black still for *lead* s, then *motion* s of moving content."""
    fps = 10
    black = _write_video(path.with_name("black.mp4"), source="color=c=black", duration=lead, fps=fps)
    # testsrc is a continuously-animating pattern → no static run.
    moving = _write_video(path.with_name("moving.mp4"), source="testsrc", duration=motion, fps=fps)
    concat = path.with_name("concat.txt")
    concat.write_text(f"file '{black}'\nfile '{moving}'\n", encoding="utf-8")
    subprocess.run(
        [_ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(path)],
        check=True,
        capture_output=True,
    )
    return path


class TestDefaultBudget:
    """The dead-lead budget is a named constant in the sensible 2-3s range."""

    def test_default_is_a_small_named_constant(self) -> None:
        assert 2.0 <= DEFAULT_MAX_DEAD_LEAD_SECONDS <= 3.0


@_needs_ffmpeg
class TestLeadingDeadFrames:
    """A long blank pre-roll FAILS; a tight (prompt-start) video PASSES."""

    def test_long_blank_lead_fails(self, tmp_path: Path) -> None:
        video = _blank_lead_then_motion(tmp_path / "blank_lead.mp4", lead=8.0, motion=4.0)
        report = check_video_evidence(video)
        assert report.skipped is False
        assert report.ok is False
        assert report.dead_lead_seconds >= 5.0
        assert report.duration >= 11.0

    def test_tight_video_passes(self, tmp_path: Path) -> None:
        # testsrc animates from the first frame → essentially zero static lead.
        video = _write_video(tmp_path / "tight.mp4", source="testsrc", duration=8.0)
        report = check_video_evidence(video)
        assert report.skipped is False
        assert report.ok is True
        assert report.dead_lead_seconds < DEFAULT_MAX_DEAD_LEAD_SECONDS

    def test_budget_is_configurable(self, tmp_path: Path) -> None:
        video = _blank_lead_then_motion(tmp_path / "lead4.mp4", lead=4.0, motion=4.0)
        # A 4s blank lead fails the default budget but passes a generous one.
        assert check_video_evidence(video).ok is False
        assert check_video_evidence(video, max_dead_lead_seconds=6.0).ok is True


@_needs_ffmpeg
class TestRaisingForm:
    """The raising form is the gate-facing surface — it raises only on a real failure."""

    def test_raises_naming_the_dead_lead_on_a_blank_video(self, tmp_path: Path) -> None:
        video = _blank_lead_then_motion(tmp_path / "blank_raise.mp4", lead=8.0, motion=4.0)
        with pytest.raises(VideoEvidenceError) as exc:
            check_video_evidence(video, raising=True)
        message = str(exc.value)
        assert "dead" in message.lower()
        # The message names the measured dead-lead seconds so the refusal is actionable.
        assert any(char.isdigit() for char in message)

    def test_does_not_raise_on_a_tight_video(self, tmp_path: Path) -> None:
        video = _write_video(tmp_path / "tight_raise.mp4", source="testsrc", duration=8.0)
        # No exception — a passing video clears the raising form.
        check_video_evidence(video, raising=True)


class TestFfmpegMissingDegradesGracefully:
    """ffmpeg-missing skips with a clear signal — it never crashes the post path."""

    def test_missing_ffmpeg_returns_a_skipped_report(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        video = tmp_path / "any.mp4"
        video.write_bytes(b"not really a video")
        monkeypatch.setattr("teatree.core.evidence.video_evidence.shutil.which", lambda _name: None)
        report = check_video_evidence(video)
        assert isinstance(report, VideoEvidenceReport)
        assert report.skipped is True
        # A skip is treated as OK (the gate must not refuse a post just because ffmpeg is absent).
        assert report.ok is True
        assert "ffmpeg" in (report.detail or "").lower()

    def test_missing_ffmpeg_does_not_raise_even_in_raising_form(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        video = tmp_path / "any.mp4"
        video.write_bytes(b"not really a video")
        monkeypatch.setattr("teatree.core.evidence.video_evidence.shutil.which", lambda _name: None)
        # The raising form must still NOT raise when ffmpeg is missing — skip, not crash.
        report = check_video_evidence(video, raising=True)
        assert report.skipped is True


class TestMissingFile:
    """A referenced video that does not exist fails loud (the post path must not 'pass' it)."""

    @_needs_ffmpeg
    def test_absent_path_raises(self, tmp_path: Path) -> None:
        with pytest.raises(VideoEvidenceError):
            check_video_evidence(tmp_path / "does_not_exist.mp4", raising=True)

    @_needs_ffmpeg
    def test_absent_path_non_raising_returns_failed_report(self, tmp_path: Path) -> None:
        report = check_video_evidence(tmp_path / "does_not_exist.mp4")
        assert report.ok is False
        assert report.skipped is False
        assert "does not exist" in (report.detail or "")


class TestStderrParsers:
    """The ffmpeg-log parsers are pure string logic — exercised without invoking ffmpeg."""

    def test_black_run_parsed_from_log(self) -> None:
        stderr = "[blackdetect @ 0x1] black_start:0 black_end:5.2 black_duration:5.2"
        assert _ve._parse_black_runs(stderr) == [(0.0, 5.2)]

    def test_black_line_without_a_pair_is_dropped(self) -> None:
        # A black_start with no black_end (truncated log line) yields no run.
        assert _ve._parse_black_runs("[blackdetect] black_start:0") == []
        assert _ve._parse_black_runs("no black here") == []

    def test_freeze_run_paired_across_two_lines(self) -> None:
        stderr = (
            "frame: lavfi.freezedetect.freeze_start: 0\n"
            "frame: lavfi.freezedetect.freeze_duration: 6\n"
            "frame: lavfi.freezedetect.freeze_end: 6.0\n"
        )
        assert _ve._parse_freeze_runs(stderr) == [(0.0, 6.0)]

    def test_freeze_end_without_a_start_is_ignored(self) -> None:
        # An end with no pending start (parser saw end first) yields no run.
        assert _ve._parse_freeze_runs("lavfi.freezedetect.freeze_end: 6.0") == []

    def test_freeze_end_with_unparseable_value_clears_the_pending_start(self) -> None:
        # A start followed by a malformed end value yields no run, and the pending
        # start is reset (the end branch resets it even when the value is unparsable).
        stderr = "lavfi.freezedetect.freeze_start: 0\nlavfi.freezedetect.freeze_end: notanumber\n"
        assert _ve._parse_freeze_runs(stderr) == []

    def test_field_absent_returns_none(self) -> None:
        assert _ve._field("no key here", "black_start:") is None

    def test_field_non_float_returns_none(self) -> None:
        assert _ve._field("black_start:notanumber", "black_start:") is None

    def test_field_trailing_with_no_token_returns_none(self) -> None:
        assert _ve._field("black_start:", "black_start:") is None

    def test_leading_run_skips_a_run_starting_after_tolerance(self) -> None:
        # A run that begins well after t=0 is NOT leading dead time.
        assert _ve._leading_run([(10.0, 12.0)]) == pytest.approx(0.0)

    def test_leading_run_picks_the_run_at_t0(self) -> None:
        assert _ve._leading_run([(0.0, 5.0), (10.0, 12.0)]) == pytest.approx(5.0)


class TestProbeDurationFallbacks:
    """Duration probing degrades to 0.0 on both a failed command and unparsable output."""

    def test_command_failure_yields_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(*_args: object, **_kwargs: object) -> object:
            raise CommandFailedError(["ffprobe"], 1, "", "boom")

        monkeypatch.setattr(_ve, "run_allowed_to_fail", _raise)
        assert _ve._probe_duration("ffprobe", Path("x.mp4")) == pytest.approx(0.0)

    def test_unparseable_stdout_yields_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Result:
            stdout = "N/A"

        monkeypatch.setattr(_ve, "run_allowed_to_fail", lambda *_a, **_k: _Result())
        assert _ve._probe_duration("ffprobe", Path("x.mp4")) == pytest.approx(0.0)
