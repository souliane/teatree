"""Deterministic leading-dead-frame check for test-plan video evidence.

The recurrence this forecloses: an E2E test-plan VIDEO with ~40s of blank/static
pre-roll (out of a ~70s recording) and an unclear final frame was posted to a
customer ticket, and neither the author nor the e2e-review gate caught it. The
``e2e write-test-plan`` path machine-enforces SCREENSHOT quality
(:mod:`teatree.core.evidence.test_plan_validation`: red-box pixel gate + md5 dedup +
media-kind), but had ZERO quality check on the recorded video, and
``scripts/analyze_video.py`` only dumped frames for manual viewing with no
verdict.

This module is the deterministic substitute, mirroring the
:mod:`teatree.core.evidence.test_plan_validation` / :mod:`teatree.core.evidence.doc_evidence`
shape — a dedicated error subclass, a frozen report dataclass, and pure logic
over an on-disk path with a clear refusal message. It is NOT an LLM check: it
shells ``ffprobe``/``ffmpeg`` to measure the LEADING static-or-blank run from
``t=0`` (the dead pre-roll an author records when they start the capture before
the interaction begins) and refuses when that run exceeds the configured budget.

Detection
    The leading dead run is the time from ``t=0`` until the first frame that is
    BOTH non-blank AND has begun to change — i.e. the recording has actually
    started doing something. ``ffmpeg``'s ``blackdetect`` filter reports blank
    (near-black) runs and ``freezedetect`` reports static (near-identical
    consecutive) runs; a run that starts at ``t=0`` on either filter is dead
    lead. The dead lead is the longer of the two leading runs.

ffmpeg-missing degrades, never crashes (the post path safety property)
    ``ffmpeg`` is an OPTIONAL tool here, exactly as
    ``scripts/analyze_video.py`` treats it. When ``ffmpeg``/``ffprobe`` is not on
    ``PATH`` (a CI runner without it, a minimal container), the check returns a
    ``skipped`` report that reads as OK rather than raising — refusing every
    test-plan post on a host that simply lacks ffmpeg would be worse than the
    missing check. A skip carries a clear ``detail`` so the absence is visible.

Fail-loud on a real problem
    A genuinely-broken referenced video (absent file, unprobeable) and an
    over-budget dead lead both surface via :class:`VideoEvidenceError` in the
    raising form the gate uses — never a silent pass.
"""

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from teatree.utils.run import CommandFailedError, TimeoutExpired, run_allowed_to_fail

# The dead-lead budget: a recording may carry a small amount of settle/start
# time, but more than this is dead pre-roll the author should have trimmed. A
# sensible 2-3s window — generous enough for a real page settle, tight enough to
# catch the ~40s-blank-lead recurrence this module exists for.
DEFAULT_MAX_DEAD_LEAD_SECONDS = 2.5

# The whole-film budget, as a fraction of duration. The dead-LEAD budget above says
# nothing about a recording that comes alive briefly and then sits still for a minute,
# which is the near-empty film a reviewer reads as "nothing happened". Generous enough
# that a demo with long reading pauses still passes.
DEFAULT_MAX_DEAD_FRACTION = 0.75

# A frame is "blank" below this average luminance (0-1) and "static" when
# consecutive frames differ by less than this noise floor. These are ffmpeg's
# own documented defaults for blackdetect (pixel/picture black thresholds) and a
# freezedetect noise floor chosen to catch a truly-static screen without
# tripping on subtle anti-alias jitter.
_BLACK_PIXEL_THRESHOLD = 0.10
_BLACK_PICTURE_RATIO = 0.98
_FREEZE_NOISE_DB = "-60dB"

# A leading run that starts within this many seconds of t=0 counts as "from the
# start" — a freeze/black run whose start is slightly after 0 (the first frame
# is decoded at a tiny non-zero pts) is still leading dead time.
_LEADING_START_TOLERANCE = 0.5

# ffprobe/ffmpeg invocations are bounded — a probe should be near-instant; this
# backstop keeps a pathological input from hanging the post path.
_PROBE_TIMEOUT_SECONDS = 60.0


class VideoEvidenceError(ValueError):
    """A test-plan video failed a hard evidence check — the post must NOT publish.

    Raised by :func:`check_video_evidence` (raising form) for an over-budget
    leading dead run, a missing referenced file, or an unprobeable video. The
    message names the measured dead-lead seconds (or the broken file) so the
    refusal is actionable. A failed check runs before any upload, so it burns no
    on-behalf approval and writes no note. ffmpeg-being-absent is NOT a failure —
    that path returns a ``skipped`` report instead of raising.
    """

    __test__ = False  # not a pytest test class


@dataclass(frozen=True, slots=True)
class VideoEvidenceReport:
    """The verdict of a leading-dead-frame check over one video.

    ``dead_lead_seconds`` is the measured leading static-or-blank run from
    ``t=0``; ``dead_total_seconds`` is every dead stretch in the film, overlaps
    counted once; ``duration`` is the video's total length; ``ok`` is the boolean
    verdict (dead lead within budget AND the film not overwhelmingly dead).
    ``skipped`` is ``True`` only when the check could not run because
    ffmpeg/ffprobe is absent — a skip reads as ``ok`` so the gate never refuses a
    post merely because the host lacks ffmpeg.
    ``detail`` carries a human-readable reason for a skip or a failure.
    """

    ok: bool
    dead_lead_seconds: float
    duration: float
    max_dead_lead_seconds: float
    dead_total_seconds: float = 0.0
    max_dead_fraction: float = DEFAULT_MAX_DEAD_FRACTION
    skipped: bool = False
    detail: str | None = None


def _ffmpeg_tools() -> tuple[str, str] | None:
    """Return ``(ffmpeg, ffprobe)`` paths, or ``None`` when either is absent."""
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        return None
    return ffmpeg, ffprobe


def _probe_duration(ffprobe: str, video: Path) -> float:
    """Read the container duration (seconds) via ffprobe; 0.0 when the container declares none.

    A non-zero exit (or a timeout) means the file could not be READ at all, which
    is a different fact from a container that simply declares no duration — a
    corrupt recording exiting non-zero measured as ``0.0`` and passed the gate. It
    is surfaced as the gate's own :class:`VideoEvidenceError`, never a raw
    subprocess traceback. A ``.webm`` Playwright capture legitimately carries no
    ``format=duration`` and exits ``0`` with empty stdout, so that stays ``0.0``
    (the tolerated unknown ``_detect_dead_lead`` already guards with ``duration > 0``).
    """
    try:
        result = run_allowed_to_fail(
            [ffprobe, "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
            expected_codes=(0,),
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except (CommandFailedError, TimeoutExpired) as exc:
        msg = (
            f"Test plan refused: video {video.name} could not be read by ffprobe — "
            f"the recording cannot be checked for dead pre-roll."
        )
        raise VideoEvidenceError(msg) from exc
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def _leading_run(starts_and_ends: list[tuple[float, float]]) -> float:
    """Length of the run that begins at (or within tolerance of) ``t=0``; else 0.0."""
    for start, end in starts_and_ends:
        if start <= _LEADING_START_TOLERANCE:
            return max(0.0, end - start)
    return 0.0


def _parse_black_runs(stderr: str, duration: float) -> list[tuple[float, float]]:
    """Parse ``blackdetect`` ``black_start:..`` / ``black_end:..`` pairs from stderr.

    A start with no end ran to EOF; it closes at *duration* rather than being dropped —
    the run that never recovers is the worst recording, so discarding it measured a film
    that is dead to its end as carrying no dead time at all.
    """
    runs: list[tuple[float, float]] = []
    for line in stderr.splitlines():
        if "black_start" not in line:
            continue
        start = _field(line, "black_start:")
        if start is None:
            continue
        end = _field(line, "black_end:")
        runs.append((start, end if end is not None else duration))
    return _closed_runs(runs)


def _parse_freeze_runs(stderr: str, duration: float) -> list[tuple[float, float]]:
    """Parse ``freezedetect`` ``freeze_start`` / ``freeze_end`` pairs from stderr.

    ffmpeg emits no ``freeze_end`` for a freeze that runs to EOF (measured), so a pending
    start closes at *duration* — see :func:`_parse_black_runs`.
    """
    runs: list[tuple[float, float]] = []
    pending_start: float | None = None
    for line in stderr.splitlines():
        if "lavfi.freezedetect.freeze_start" in line:
            if pending_start is not None:
                runs.append((pending_start, duration))
            pending_start = _field(line, "freeze_start:")
        elif "lavfi.freezedetect.freeze_end" in line and pending_start is not None:
            end = _field(line, "freeze_end:")
            runs.append((pending_start, end if end is not None else duration))
            pending_start = None
    if pending_start is not None:
        runs.append((pending_start, duration))
    return _closed_runs(runs)


def _closed_runs(runs: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Drop runs an unknown duration left unclosable, so they never read as negative time."""
    return [(start, end) for start, end in runs if end > start]


def _field(line: str, key: str) -> float | None:
    """Extract the float that follows ``key`` (``key:VALUE``) in a log line."""
    idx = line.find(key)
    if idx < 0:
        return None
    tail = line[idx + len(key) :].strip()
    token = tail.split()[0] if tail.split() else ""
    try:
        return float(token)
    except ValueError:
        return None


def _merged_span(runs: list[tuple[float, float]]) -> float:
    """Total seconds covered by ``runs``, overlaps counted once.

    A blank stretch is also a static one, so both detectors report it; summing them
    would measure a mostly-live recording as more dead than it is long.
    """
    total = 0.0
    covered_to = float("-inf")
    for start, end in sorted(runs):
        if end <= covered_to:
            continue
        total += end - max(start, covered_to)
        covered_to = end
    return total


@dataclass(frozen=True, slots=True)
class _DeadTime:
    """One recording's measured dead time: the leading run, the union of every run, the length."""

    lead: float
    total: float
    duration: float


def _detect_dead(ffmpeg: str, video: Path, duration: float) -> _DeadTime:
    """Measure the leading dead run and the film's TOTAL dead time (seconds).

    Runs ``blackdetect`` and ``freezedetect`` in one pass and takes the longer of
    the two leading runs — a recording can lead with a black screen, a frozen
    first frame, or both, and any of those is dead pre-roll. The total is the union
    of every run either detector reported, which is what separates a recording that
    merely starts slowly from one that is dead for its whole length.
    """
    filtergraph = (
        f"blackdetect=d=0.1:pix_th={_BLACK_PIXEL_THRESHOLD}:pic_th={_BLACK_PICTURE_RATIO},"
        f"freezedetect=n={_FREEZE_NOISE_DB}:d=0.5"
    )
    try:
        result = run_allowed_to_fail(
            [ffmpeg, "-i", str(video), "-vf", filtergraph, "-an", "-f", "null", "-"],
            expected_codes=(0,),
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except TimeoutExpired as exc:
        # A hung/pathological ffmpeg pass must surface as the gate's own typed
        # failure, not a raw ``subprocess.TimeoutExpired`` traceback out of the
        # post path.
        msg = (
            f"Test plan refused: analysing video {video.name} timed out after "
            f"{_PROBE_TIMEOUT_SECONDS:.0f}s — the recording could not be checked for dead pre-roll."
        )
        raise VideoEvidenceError(msg) from exc
    except CommandFailedError as exc:
        # A non-zero ffmpeg exit yields no detector output, which parsed as a
        # 0.0s dead lead — the corrupt recording then satisfied the gate.
        msg = (
            f"Test plan refused: video {video.name} could not be analysed by ffmpeg — "
            f"the recording cannot be checked for dead pre-roll."
        )
        raise VideoEvidenceError(msg) from exc
    stderr = result.stderr
    black_runs = _parse_black_runs(stderr, duration)
    freeze_runs = _parse_freeze_runs(stderr, duration)
    dead = max(_leading_run(black_runs), _leading_run(freeze_runs))
    total = _merged_span([*black_runs, *freeze_runs])
    # A run that ffmpeg reports as open-ended (freeze to EOF) is capped at duration.
    if duration > 0:
        return _DeadTime(lead=min(dead, duration), total=min(total, duration), duration=duration)
    return _DeadTime(lead=dead, total=total, duration=duration)


def validate_manifest_videos(videos: list[Path], *, skip: bool = False) -> None:
    """Raise on the first manifest video that opens with excessive blank/static pre-roll.

    The post-path entry point mirroring
    :func:`teatree.core.evidence.test_plan_validation.validate_test_plan_images`: it runs
    the raising :func:`check_video_evidence` over every referenced video and
    re-raises the first :class:`VideoEvidenceError` so the caller's single
    validation-error arm aborts the post before any upload. ``skip=True`` is the
    user-authorised bypass (the agent never sets it itself); an ffmpeg-absent host
    skips each video cleanly inside :func:`check_video_evidence`.

    A skipped video is NAMED on stderr. Silence on this path is the caller's only
    signal, so an unmeasured film and a measured one would otherwise leave the
    identical trace — the shape that let an empty audit export report green.
    """
    if skip:
        return
    for video in videos:
        report = check_video_evidence(video, raising=True)
        if report.skipped:
            sys.stderr.write(f"NOTE: video {video.name} was NOT checked — {report.detail}\n")


def check_video_evidence(
    video: Path,
    *,
    max_dead_lead_seconds: float = DEFAULT_MAX_DEAD_LEAD_SECONDS,
    max_dead_fraction: float = DEFAULT_MAX_DEAD_FRACTION,
    raising: bool = False,
) -> VideoEvidenceReport:
    """Check *video* for blank/static pre-roll and for being dead overall, deterministically.

    Shells ffprobe/ffmpeg to measure the leading dead run from ``t=0`` and the
    film's total dead time, comparing them to ``max_dead_lead_seconds`` and
    ``max_dead_fraction`` respectively. Returns a
    :class:`VideoEvidenceReport`; in the ``raising=True`` gate form a genuine
    failure (over-budget dead lead, missing/unprobeable file) raises
    :class:`VideoEvidenceError` instead of returning ``ok=False``.

    ffmpeg/ffprobe absent → a ``skipped`` report that reads as ``ok`` (the post
    path must not refuse every video just because the host lacks ffmpeg); the
    raising form does NOT raise on a skip. A genuinely-missing referenced file,
    and a file present but UNREADABLE (either tool exits non-zero or times out),
    are hard failures — the post path must not silently pass a video it could
    not measure — surfaced in both forms.
    """
    tools = _ffmpeg_tools()
    if tools is None:
        detail = "ffmpeg/ffprobe not on PATH — video evidence check skipped (install ffmpeg to enable it)."
        return VideoEvidenceReport(
            ok=True,
            dead_lead_seconds=0.0,
            duration=0.0,
            max_dead_lead_seconds=max_dead_lead_seconds,
            max_dead_fraction=max_dead_fraction,
            skipped=True,
            detail=detail,
        )
    if not video.exists():
        detail = f"Test plan refused: video {video.name} does not exist."
        if raising:
            raise VideoEvidenceError(detail)
        return VideoEvidenceReport(
            ok=False,
            dead_lead_seconds=0.0,
            duration=0.0,
            max_dead_lead_seconds=max_dead_lead_seconds,
            max_dead_fraction=max_dead_fraction,
            detail=detail,
        )

    ffmpeg, ffprobe = tools
    try:
        measured = _detect_dead(ffmpeg, video, _probe_duration(ffprobe, video))
    except VideoEvidenceError as exc:
        if raising:
            raise
        return VideoEvidenceReport(
            ok=False,
            dead_lead_seconds=0.0,
            duration=0.0,
            max_dead_lead_seconds=max_dead_lead_seconds,
            max_dead_fraction=max_dead_fraction,
            detail=str(exc),
        )
    detail = _refusal(video, measured, max_dead_lead_seconds=max_dead_lead_seconds, max_dead_fraction=max_dead_fraction)
    if detail is not None and raising:
        raise VideoEvidenceError(detail)
    return VideoEvidenceReport(
        ok=detail is None,
        dead_lead_seconds=measured.lead,
        duration=measured.duration,
        max_dead_lead_seconds=max_dead_lead_seconds,
        dead_total_seconds=measured.total,
        max_dead_fraction=max_dead_fraction,
        detail=detail,
    )


def _refusal(video: Path, measured: _DeadTime, *, max_dead_lead_seconds: float, max_dead_fraction: float) -> str | None:
    """The refusal message for a measured recording, or ``None`` when it passes both budgets."""
    if measured.lead > max_dead_lead_seconds:
        return (
            f"Test plan refused: video {video.name} opens with {measured.lead:.1f}s of blank/static "
            f"pre-roll (budget {max_dead_lead_seconds:.1f}s) out of {measured.duration:.1f}s total. "
            f"Capture so the interaction starts promptly — do not record dead setup time."
        )
    if measured.duration > 0 and measured.total > measured.duration * max_dead_fraction:
        share = measured.total / measured.duration
        return (
            f"Test plan refused: video {video.name} is blank/static for {measured.total:.1f}s of its "
            f"{measured.duration:.1f}s ({share:.0%}, budget {max_dead_fraction:.0%}) — a "
            f"near-empty recording is not evidence. Trim it to the interaction it demonstrates."
        )
    return None
