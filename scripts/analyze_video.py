"""Decompose a video into frames for visual analysis, or verify its quality.

Default mode extracts frames from a video file at a fixed interval using
ffmpeg, producing numbered PNG images that an AI agent can read and analyze.

``--verify`` mode runs the deterministic ``teatree.core.evidence.video_evidence`` check
(leading blank/static pre-roll budget) and exits non-zero on failure — the same
gate ``e2e post-test-plan`` machine-enforces — so a human or agent can check a
recording directly before posting it.

Supports local files and URLs (downloaded first via curl).
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import typer

app = typer.Typer(add_completion=False)


def _check_ffmpeg() -> str:
    """Return ffmpeg path or exit with install instructions."""
    path = shutil.which("ffmpeg")
    if not path:
        print(
            "Error: ffmpeg not found.\nInstall with: brew install ffmpeg",
            file=sys.stderr,
        )
        raise typer.Exit(1)
    return path


def _get_video_duration(ffmpeg_path: str, video_path: Path) -> float:
    """Get video duration in seconds using ffprobe."""
    ffprobe = ffmpeg_path.replace("ffmpeg", "ffprobe")
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "quiet",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(video_path),
        ],
        capture_output=True,
        text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def _download_url(url: str, dest: Path) -> Path:
    """Download a URL to a local file."""
    suffix = Path(url.split("?", maxsplit=1)[0]).suffix or ".mp4"
    local = dest / f"input{suffix}"
    result = subprocess.run(
        ["curl", "-sL", "-o", str(local), url],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Error downloading {url}: {result.stderr}", file=sys.stderr)
        raise typer.Exit(1)
    if not local.exists() or local.stat().st_size == 0:
        print(f"Error: downloaded file is empty: {local}", file=sys.stderr)
        raise typer.Exit(1)
    return local


def _resolve_local_source(source: str) -> Path:
    """Resolve *source* to a local path, downloading a URL into a temp dir first."""
    if source.startswith(("http://", "https://")):
        _check_ffmpeg()
        tmp_dir = Path(tempfile.mkdtemp(prefix="t3_video_verify_"))
        return _download_url(source, tmp_dir)
    path = Path(source).expanduser().resolve()
    if not path.exists():
        print(f"Error: file not found: {path}", file=sys.stderr)
        raise typer.Exit(1)
    return path


def _run_verify(source: str, *, max_dead_lead: float) -> None:
    """Run the deterministic video-evidence check and exit non-zero on failure."""
    from teatree.core.evidence.video_evidence import DEFAULT_MAX_DEAD_LEAD_SECONDS, check_video_evidence

    video_path = _resolve_local_source(source)
    budget = max_dead_lead if max_dead_lead > 0 else DEFAULT_MAX_DEAD_LEAD_SECONDS
    report = check_video_evidence(video_path, max_dead_lead_seconds=budget)
    if report.skipped:
        print(f"SKIPPED: {report.detail}", file=sys.stderr)
        return
    print(f"Video: {video_path.name}")
    print(f"Duration: {report.duration:.1f}s")
    print(f"Leading dead pre-roll: {report.dead_lead_seconds:.1f}s (budget {budget:.1f}s)")
    if report.ok:
        print("PASS: no excessive blank/static pre-roll.")
        return
    print(f"FAIL: {report.detail}", file=sys.stderr)
    raise typer.Exit(1)


@app.command()
def main(  # noqa: PLR0913, PLR0917
    source: str = typer.Argument(help="Video file path or URL"),
    interval: float = typer.Option(
        1.0,
        "--interval",
        "-i",
        help="Seconds between extracted frames (default: 1.0)",
    ),
    max_frames: int = typer.Option(
        30,
        "--max-frames",
        "-m",
        help="Maximum number of frames to extract (default: 30)",
    ),
    output_dir: str = typer.Option(
        "",
        "--output",
        "-o",
        help="Output directory (default: auto-created temp dir)",
    ),
    scene_detect: bool = typer.Option(
        False,
        "--scene",
        "-s",
        help="Use scene change detection instead of fixed interval",
    ),
    threshold: float = typer.Option(
        0.3,
        "--threshold",
        "-t",
        help="Scene change threshold 0.0-1.0 (only with --scene)",
    ),
    verify: bool = typer.Option(
        False,
        "--verify",
        help="Run the deterministic leading-dead-frame check and exit non-zero on failure",
    ),
    max_dead_lead: float = typer.Option(
        0.0,
        "--max-dead-lead",
        help="Override the leading blank/static pre-roll budget in seconds (only with --verify)",
    ),
) -> None:
    """Decompose a video into frames for AI agent analysis, or verify its quality.

    Default: extracts frames at a fixed interval (default: 1 per second) or at
    scene changes, prints numbered PNG paths the agent can Read.

    ``--verify``: runs the deterministic ``teatree.core.evidence.video_evidence`` check
    (leading blank/static pre-roll budget) and exits non-zero when the recording
    opens with too much dead pre-roll — the same gate ``e2e post-test-plan``
    enforces.
    """
    if verify:
        _run_verify(source, max_dead_lead=max_dead_lead)
        return

    ffmpeg_path = _check_ffmpeg()

    # Resolve output directory
    if output_dir:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        tmp_dir = None
    else:
        tmp_dir = tempfile.mkdtemp(prefix="t3_video_")
        out = Path(tmp_dir)

    # Handle URL vs local file
    video_path: Path
    if source.startswith(("http://", "https://")):
        print(f"Downloading: {source}")
        video_path = _download_url(source, out)
    else:
        video_path = Path(source).expanduser().resolve()
        if not video_path.exists():
            print(f"Error: file not found: {video_path}", file=sys.stderr)
            raise typer.Exit(1)

    # Get duration for summary
    duration = _get_video_duration(ffmpeg_path, video_path)

    # Build ffmpeg command
    frame_pattern = str(out / "frame_%04d.png")

    if scene_detect:
        # Scene change detection: extract frames where scene changes
        vf = f"select='gt(scene\\,{threshold})',showinfo"
        cmd = [
            ffmpeg_path,
            "-i",
            str(video_path),
            "-vf",
            vf,
            "-vsync",
            "vfr",
            "-frames:v",
            str(max_frames),
            frame_pattern,
            "-y",
        ]
    else:
        # Fixed interval extraction
        cmd = [
            ffmpeg_path,
            "-i",
            str(video_path),
            "-vf",
            f"fps=1/{interval}",
            "-frames:v",
            str(max_frames),
            frame_pattern,
            "-y",
        ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ffmpeg error:\n{result.stderr}", file=sys.stderr)
        raise typer.Exit(1)

    # Collect and sort output frames
    frames = sorted(out.glob("frame_*.png"))

    if not frames:
        print("No frames extracted — video may be too short or corrupt.", file=sys.stderr)
        raise typer.Exit(1)

    # Print summary
    mode = f"scene detection (threshold={threshold})" if scene_detect else f"every {interval}s"
    print(f"Video: {video_path.name}")
    print(f"Duration: {duration:.1f}s")
    print(f"Mode: {mode}")
    print(f"Frames extracted: {len(frames)}")
    print(f"Output directory: {out}")
    print()
    print("--- Frame paths (use Read tool to analyze) ---")
    for f in frames:
        # Calculate approximate timestamp
        idx = int(f.stem.split("_")[1]) - 1
        ts = idx * interval if not scene_detect else -1
        ts_str = f" ({ts:.1f}s)" if ts >= 0 else ""
        print(f"  {f}{ts_str}")


if __name__ == "__main__":
    app()
