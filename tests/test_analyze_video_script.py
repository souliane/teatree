"""Tests for ``scripts/analyze_video.py`` — pure helpers and ffmpeg integration.

Covers the defects from souliane/teatree#3116: silent truncation of long
videos, no frame scaling, no crop/contact-sheet views, and no authenticated
fetch for forge upload URLs. The pure helpers (interval derivation, coverage
accounting, filter-graph building, fetch routing) are unit-tested; the
end-to-end frame pipeline is exercised against a generated clip when ffmpeg is
present.
"""

import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import analyze_video as av

_HAS_FFMPEG = shutil.which("ffmpeg") is not None


class TestEffectiveInterval:
    """Deriving a sampling interval that spans the whole video by default (#3116 defect 1)."""

    def test_auto_interval_spans_full_duration(self):
        """No user interval → derive one so max_frames span the whole video, not just the first 30s."""
        assert av._effective_interval(51.4, 30, 0.0) == pytest.approx(51.4 / 30)

    def test_user_interval_is_honored(self):
        assert av._effective_interval(51.4, 30, 0.75) == pytest.approx(0.75)

    def test_unprobeable_duration_falls_back(self):
        assert av._effective_interval(0.0, 30, 0.0) == pytest.approx(1.0)


class TestCoverage:
    """Loud coverage accounting so partial coverage never reads as full (#3116 defect 1)."""

    def test_dropped_tail_is_reported(self):
        covered_end, dropped = av._coverage(51.4, 1.0, 30)
        assert covered_end == pytest.approx(29.0)
        assert dropped == 22

    def test_full_coverage_drops_nothing(self):
        covered_end, dropped = av._coverage(6.0, 0.2, 30)
        assert dropped == 0
        assert covered_end == pytest.approx(5.8)


class TestFrameFilter:
    """Frame filter-graph: scale + crop composition (#3116 defects 3 & 4)."""

    def test_fixed_interval_with_default_scale(self):
        vf = av._frame_filter(interval=1.0, scale=1280, crop="", scene=False, threshold=0.3)
        assert vf == "fps=1/1.0,scale='min(1280,iw)':-2"

    def test_scale_zero_keeps_native_resolution(self):
        vf = av._frame_filter(interval=1.0, scale=0, crop="", scene=False, threshold=0.3)
        assert vf == "fps=1/1.0"

    def test_top_bar_crop_preset(self):
        vf = av._frame_filter(interval=1.0, scale=0, crop="top-bar", scene=False, threshold=0.3)
        assert vf == "fps=1/1.0,crop=iw:ih*0.07:0:0"

    def test_scene_mode_uses_select_filter(self):
        vf = av._frame_filter(interval=1.0, scale=0, crop="", scene=True, threshold=0.4)
        assert vf.startswith("select='gt(scene")
        assert "0.4" in vf


class TestCropExpression:
    def test_top_bar_preset(self):
        assert av._crop_expr("top-bar") == "crop=iw:ih*0.07:0:0"

    def test_explicit_geometry(self):
        assert av._crop_expr("300:80:0:0") == "crop=300:80:0:0"

    def test_empty_is_no_crop(self):
        assert av._crop_expr("") == ""

    def test_invalid_geometry_raises(self):
        with pytest.raises(ValueError, match="crop"):
            av._crop_expr("nonsense")


class TestTileFilter:
    """Contact-sheet grid parsing, ROWSxCOLS → ffmpeg tile=COLSxROWS (#3116 defect 4)."""

    def test_grid_is_rows_by_cols(self):
        assert av._tile_filter("6x5") == "tile=5x6"

    def test_single_column_vertical_stack(self):
        assert av._tile_filter("30x1") == "tile=1x30"

    def test_invalid_grid_raises(self):
        with pytest.raises(ValueError, match="contact-sheet"):
            av._tile_filter("5")


class TestFetchPlan:
    """Authenticated fetch routing for forge upload URLs (#3116 defect 5)."""

    _SECRET = "deadbeef" * 4  # a valid 32-hex upload secret, low-entropy so it is not a real credential

    def test_gitlab_upload_uses_glab_api(self, tmp_path):
        url = f"https://gitlab.com/mygroup/myproj/uploads/{self._SECRET}/screen.mov"
        argv, capture_stdout = av._fetch_plan(url, tmp_path / "input.mov")
        assert argv[:2] == ["glab", "api"]
        assert argv[-1] == f"projects/mygroup%2Fmyproj/uploads/{self._SECRET}/screen.mov"
        assert "--hostname" not in argv
        assert capture_stdout is True

    def test_self_managed_gitlab_passes_hostname(self, tmp_path):
        url = f"https://gitlab.example.com/team/app/uploads/{self._SECRET}/rec.mov"
        argv, _ = av._fetch_plan(url, tmp_path / "input.mov")
        assert "--hostname" in argv
        assert argv[argv.index("--hostname") + 1] == "gitlab.example.com"

    def test_gitlab_upload_with_subgroups(self, tmp_path):
        url = f"https://gitlab.com/grp/sub/app/uploads/{self._SECRET}/a.mov"
        argv, _ = av._fetch_plan(url, tmp_path / "input.mov")
        assert argv[-1] == f"projects/grp%2Fsub%2Fapp/uploads/{self._SECRET}/a.mov"

    def test_github_attachment_uses_authenticated_curl(self, tmp_path):
        url = "https://github.com/user-attachments/assets/deadbeef-1234"
        argv, capture_stdout = av._fetch_plan(url, tmp_path / "input.mp4", gh_token="TESTTOKEN")
        assert argv[0] == "curl"
        assert "Authorization: Bearer TESTTOKEN" in argv
        assert capture_stdout is False

    def test_github_without_token_is_plain_curl(self, tmp_path):
        url = "https://github.com/user-attachments/assets/deadbeef-1234"
        argv, _ = av._fetch_plan(url, tmp_path / "input.mp4", gh_token=None)
        assert argv[0] == "curl"
        assert not any(a.startswith("Authorization") for a in argv)

    def test_signed_githubusercontent_is_plain_curl(self, tmp_path):
        url = "https://private-user-images.githubusercontent.com/x/y.mov?jwt=abc"
        argv, _ = av._fetch_plan(url, tmp_path / "input.mov", gh_token="TESTTOKEN")
        assert argv[0] == "curl"
        assert not any(a.startswith("Authorization") for a in argv)

    def test_plain_url_uses_curl(self, tmp_path):
        argv, capture_stdout = av._fetch_plan("https://example.com/clip.mp4", tmp_path / "input.mp4")
        assert argv[0] == "curl"
        assert capture_stdout is False


def _make_clip(path: Path, *, duration: int, size: str = "320x240") -> None:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={duration}:size={size}:rate=24",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            str(path),
            "-y",
        ],
        check=True,
    )


def _run_script(*args: str) -> subprocess.CompletedProcess[str]:
    script = Path(__file__).resolve().parent.parent / "scripts" / "analyze_video.py"
    return subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True, check=False)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
class TestFramePipeline:
    """End-to-end frame extraction over a generated clip (#3116 defects 1, 3, 4)."""

    def test_auto_interval_covers_full_duration(self, tmp_path):
        video = tmp_path / "clip.mp4"
        _make_clip(video, duration=6)
        out = tmp_path / "frames"
        result = _run_script(str(video), "--output", str(out))
        assert result.returncode == 0, result.stderr
        assert "Coverage:" in result.stdout
        frames = sorted(out.glob("frame_*.png"))
        assert len(frames) >= 20, result.stdout

    def test_default_scale_shrinks_frames(self, tmp_path):
        video = tmp_path / "clip.mp4"
        _make_clip(video, duration=3, size="1920x1080")
        out = tmp_path / "frames"
        result = _run_script(str(video), "--output", str(out), "--scale", "640")
        assert result.returncode == 0, result.stderr
        frames = sorted(out.glob("frame_*.png"))
        assert frames
        width = _png_width(frames[0])
        assert width == 640, width

    def test_explicit_small_interval_warns_on_dropped_tail(self, tmp_path):
        video = tmp_path / "clip.mp4"
        _make_clip(video, duration=6)
        out = tmp_path / "frames"
        result = _run_script(str(video), "--output", str(out), "--interval", "0.1", "--max-frames", "10")
        assert result.returncode == 0, result.stderr
        assert "WARNING" in result.stdout
        assert "dropped" in result.stdout.lower()

    def test_contact_sheet_is_written(self, tmp_path):
        video = tmp_path / "clip.mp4"
        _make_clip(video, duration=4)
        out = tmp_path / "frames"
        result = _run_script(str(video), "--output", str(out), "--scale", "200", "--contact-sheet", "3x2")
        assert result.returncode == 0, result.stderr
        sheet = out / "contact_sheet.png"
        assert sheet.exists(), result.stdout


def _png_width(path: Path) -> int:
    data = path.read_bytes()
    return struct.unpack(">I", data[16:20])[0]
