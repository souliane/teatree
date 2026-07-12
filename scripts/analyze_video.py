"""Decompose a video into frames for visual analysis, or verify its quality.

Default mode extracts frames from a video file at a fixed interval using
ffmpeg, producing numbered PNG images that an AI agent can read and analyze.
Frames are scaled down (``--scale``) so a batch is readable without emitting
native-resolution Retina PNGs; ``--crop`` and ``--contact-sheet`` derive the
top-bar / interaction-strip views that make an analysis tractable.

When the user does not pass ``--interval``, the interval is derived from the
duration so the sampled frames span the WHOLE video — a long recording is never
silently half-read. When the user forces an interval that cannot cover the
duration within ``--max-frames``, an explicit coverage window and dropped-frame
count is printed rather than quietly stopping partway.

``--verify`` mode runs the deterministic ``teatree.core.evidence.video_evidence`` check
(leading blank/static pre-roll budget) and exits non-zero on failure — the same
gate ``e2e post-test-plan`` machine-enforces — so a human or agent can point it
at a recording (including a colleague's) before trusting it.

Supports local files and URLs. A GitLab project-upload URL is fetched through
``glab api`` and a GitHub attachment through an authenticated ``curl``, because a
plain ``curl`` against those returns login HTML rather than the file.
"""

import math
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote, urlparse

import typer

app = typer.Typer(add_completion=False)

# Requiring a long hex secret keeps a repo path merely containing ``/uploads/``
# from matching as a project-upload (``<host>/<path>/uploads/<secret>/<file>``).
_GITLAB_UPLOAD_RE = re.compile(r"^https?://([^/]+)/(.+?)/uploads/([0-9a-fA-F]{16,})/(.+)$")

_TOP_BAR_CROP = "crop=iw:ih*0.07:0:0"
_CROP_GEOMETRY_FIELDS = 4
_GRID_FIELDS = 2


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


def _effective_interval(duration: float, max_frames: int, user_interval: float) -> float:
    """Sampling interval to use: the user's when given, else one that spans the whole video.

    Deriving ``duration / max_frames`` when the user passes no ``--interval``
    makes the default cover the entire recording — the fix for a long video
    being silently read only up to ``max_frames x 1.0s``.
    """
    if user_interval > 0:
        return user_interval
    if duration <= 0 or max_frames <= 0:
        return 1.0
    return duration / max_frames


def _coverage(duration: float, interval: float, n_frames: int) -> tuple[float, int]:
    """Return ``(last_covered_second, frames_dropped_past_cap)`` for a fixed-interval run."""
    covered_end = max(0.0, (n_frames - 1) * interval)
    if duration <= 0 or interval <= 0:
        return covered_end, 0
    total_possible = math.ceil(duration / interval)
    return covered_end, max(0, total_possible - n_frames)


def _crop_expr(crop: str) -> str:
    """Translate a ``--crop`` value into an ffmpeg ``crop=`` filter, or '' for no crop."""
    if not crop:
        return ""
    if crop == "top-bar":
        return _TOP_BAR_CROP
    parts = crop.split(":")
    if len(parts) != _CROP_GEOMETRY_FIELDS or not all(p.strip() for p in parts):
        msg = f"Invalid --crop {crop!r}: use 'top-bar' or W:H:X:Y"
        raise ValueError(msg)
    return f"crop={crop}"


def _frame_filter(*, interval: float, scale: int, crop: str, scene: bool, threshold: float) -> str:
    """Build the ffmpeg ``-vf`` filter graph for frame extraction (sample → crop → scale)."""
    parts: list[str] = []
    if scene:
        parts.append(f"select='gt(scene\\,{threshold})',showinfo")
    else:
        parts.append(f"fps=1/{interval}")
    crop_expr = _crop_expr(crop)
    if crop_expr:
        parts.append(crop_expr)
    if scale > 0:
        parts.append(f"scale='min({scale},iw)':-2")
    return ",".join(parts)


def _tile_filter(grid: str) -> str:
    """Translate a ``ROWSxCOLS`` contact-sheet grid into an ffmpeg ``tile=COLSxROWS`` filter."""
    parts = grid.lower().split("x")
    bad_format = f"Invalid --contact-sheet {grid!r}: use ROWSxCOLS (e.g. 6x5)"
    if len(parts) != _GRID_FIELDS:
        raise ValueError(bad_format)
    try:
        rows, cols = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError(bad_format) from exc
    if rows <= 0 or cols <= 0:
        msg = f"Invalid --contact-sheet {grid!r}: rows and cols must be positive"
        raise ValueError(msg)
    return f"tile={cols}x{rows}"


def _gh_token() -> str | None:
    """Return the GitHub CLI auth token, or None when gh is absent/unauthenticated."""
    if shutil.which("gh") is None:
        return None
    result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
    token = result.stdout.strip()
    return token or None


def _fetch_plan(url: str, dest: Path, *, gh_token: str | None = None) -> tuple[list[str], bool]:
    """Return ``(argv, capture_stdout)`` to fetch *url* into *dest*, using forge auth where needed.

    A GitLab project-upload URL routes through ``glab api`` (its body is the raw
    file on stdout); a GitHub attachment through an authenticated ``curl`` (the
    token is sent only to github.com — curl drops it on the cross-host redirect
    to the signed asset store). Everything else is a plain ``curl -o``.
    """
    gitlab = _GITLAB_UPLOAD_RE.match(url)
    if gitlab is not None:
        host, project, secret, filename = gitlab.groups()
        filename = filename.split("?", maxsplit=1)[0]
        argv = ["glab", "api"]
        if host != "gitlab.com":
            argv += ["--hostname", host]
        argv.append(f"projects/{quote(project, safe='')}/uploads/{secret}/{filename}")
        return argv, True

    if urlparse(url).hostname == "github.com" and gh_token:
        return ["curl", "-sL", "-H", f"Authorization: Bearer {gh_token}", "-o", str(dest), url], False

    return ["curl", "-sL", "-o", str(dest), url], False


def _download_url(url: str, dest: Path) -> Path:
    """Download a URL to a local file, authenticating for GitLab/GitHub forge uploads."""
    suffix = Path(url.split("?", maxsplit=1)[0]).suffix or ".mp4"
    local = dest / f"input{suffix}"
    argv, capture_stdout = _fetch_plan(url, local, gh_token=_gh_token())
    result = subprocess.run(argv, capture_output=True)
    if result.returncode != 0:
        print(f"Error downloading {url}: {result.stderr.decode(errors='replace')}", file=sys.stderr)
        raise typer.Exit(1)
    if capture_stdout:
        local.write_bytes(result.stdout)
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


def _build_contact_sheet(ffmpeg_path: str, frames: list[Path], out: Path, grid: str) -> Path:
    """Tile *frames* into a single contact-sheet PNG under *out* and return its path."""
    tile = _tile_filter(grid)
    sheet = out / "contact_sheet.png"
    pattern = str(frames[0].parent / "frame_%04d.png")
    cmd = [ffmpeg_path, "-i", pattern, "-vf", tile, "-frames:v", "1", str(sheet), "-y"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ffmpeg contact-sheet error:\n{result.stderr}", file=sys.stderr)
        raise typer.Exit(1)
    return sheet


def _resolve_input(source: str, out: Path) -> Path:
    """Resolve *source* to a local video path, downloading a URL into *out* first."""
    if source.startswith(("http://", "https://")):
        print(f"Downloading: {source}")
        return _download_url(source, out)
    path = Path(source).expanduser().resolve()
    if not path.exists():
        print(f"Error: file not found: {path}", file=sys.stderr)
        raise typer.Exit(1)
    return path


def _extract_frames(ffmpeg_path: str, video_path: Path, out: Path, *, vf: str, ff_args: list[str]) -> list[Path]:
    """Sample frames from the recording; exit non-zero when ffmpeg errors or produces nothing."""
    cmd = [ffmpeg_path, "-i", str(video_path), "-vf", vf, *ff_args, str(out / "frame_%04d.png"), "-y"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ffmpeg error:\n{result.stderr}", file=sys.stderr)
        raise typer.Exit(1)
    frames = sorted(out.glob("frame_*.png"))
    if not frames:
        print("No frames extracted — video may be too short or corrupt.", file=sys.stderr)
        raise typer.Exit(1)
    return frames


def _print_report(  # noqa: PLR0913 — wide signature by design: each parameter is a distinct required input
    *,
    video_path: Path,
    duration: float,
    interval: float,
    scene: bool,
    threshold: float,
    scale: int,
    out: Path,
    frames: list[Path],
    sheet: Path | None,
) -> None:
    """Print the summary, the covered window (with a loud drop warning), and frame paths."""
    mode = f"scene detection (threshold={threshold})" if scene else f"every {interval:.2f}s"
    print(f"Video: {video_path.name}")
    print(f"Duration: {duration:.1f}s")
    print(f"Mode: {mode}")
    print(f"Frames extracted: {len(frames)}")
    if scale > 0:
        print(f"Scaled to: {scale}px wide (max)")
    print(f"Output directory: {out}")

    if not scene:
        covered_end, dropped = _coverage(duration, interval, len(frames))
        print(f"Coverage: 0.0s - {covered_end:.1f}s of {duration:.1f}s")
        if dropped > 0:
            print(
                f"WARNING: {dropped} frame(s) past --max-frames were dropped; "
                f"{covered_end:.1f}s-{duration:.1f}s is NOT covered. "
                f"Lower --interval, raise --max-frames, or omit --interval to span the whole video.",
            )

    if sheet is not None:
        print(f"Contact sheet: {sheet}")

    print()
    print("--- Frame paths (use Read tool to analyze) ---")
    for f in frames:
        idx = int(f.stem.split("_")[1]) - 1
        ts = idx * interval if not scene else -1
        ts_str = f" ({ts:.1f}s)" if ts >= 0 else ""
        print(f"  {f}{ts_str}")


@app.command()
def main(  # noqa: PLR0913, PLR0917 — wide signature by design: each parameter is a distinct required input
    source: str = typer.Argument(help="Video file path or URL (GitLab/GitHub upload URLs are fetched authenticated)"),
    interval: float = typer.Option(
        0.0,
        "--interval",
        "-i",
        help="Seconds between frames; 0 (default) derives one from duration to span the whole video",
    ),
    max_frames: int = typer.Option(
        30,
        "--max-frames",
        "-m",
        help="Maximum number of frames to extract (default: 30)",
    ),
    scale: int = typer.Option(
        1280,
        "--scale",
        help="Scale frames to this width in px, preserving aspect; 0 keeps native resolution (default: 1280)",
    ),
    crop: str = typer.Option(
        "",
        "--crop",
        help="Crop each frame: 'top-bar' preset (top ~7%) or explicit W:H:X:Y",
    ),
    contact_sheet: str = typer.Option(
        "",
        "--contact-sheet",
        help="Tile the sampled frames into one image, grid ROWSxCOLS (e.g. 6x5)",
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

    Default: extracts frames spanning the whole recording (interval derived from
    duration unless ``--interval`` is given), scaled to ``--scale`` px wide, and
    prints numbered PNG paths plus the covered window. ``--crop`` and
    ``--contact-sheet`` derive the top-bar / interaction-strip views.

    ``--verify``: runs the deterministic ``teatree.core.evidence.video_evidence`` check
    (leading blank/static pre-roll budget) and exits non-zero when the recording
    opens with too much dead pre-roll — the same gate ``e2e post-test-plan``
    enforces, now reachable for a colleague's video too.
    """
    if verify:
        _run_verify(source, max_dead_lead=max_dead_lead)
        return

    ffmpeg_path = _check_ffmpeg()

    if output_dir:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
    else:
        out = Path(tempfile.mkdtemp(prefix="t3_video_"))

    video_path = _resolve_input(source, out)
    duration = _get_video_duration(ffmpeg_path, video_path)
    effective_interval = _effective_interval(duration, max_frames, interval)

    vf = _frame_filter(interval=effective_interval, scale=scale, crop=crop, scene=scene_detect, threshold=threshold)
    ff_args = ["-vsync", "vfr", "-frames:v", str(max_frames)] if scene_detect else ["-frames:v", str(max_frames)]
    frames = _extract_frames(ffmpeg_path, video_path, out, vf=vf, ff_args=ff_args)
    sheet = _build_contact_sheet(ffmpeg_path, frames, out, contact_sheet) if contact_sheet else None

    _print_report(
        video_path=video_path,
        duration=duration,
        interval=effective_interval,
        scene=scene_detect,
        threshold=threshold,
        scale=scale,
        out=out,
        frames=frames,
        sheet=sheet,
    )


if __name__ == "__main__":
    app()
