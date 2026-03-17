"""Tests for analyze_video.py."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from analyze_video import _check_ffmpeg, _download_url, _get_video_duration


class TestCheckFfmpeg:
    def test_returns_path_when_found(self) -> None:
        with patch("analyze_video.shutil.which", return_value="/usr/bin/ffmpeg"):
            assert _check_ffmpeg() == "/usr/bin/ffmpeg"

    def test_exits_when_not_found(self) -> None:
        with patch("analyze_video.shutil.which", return_value=None), pytest.raises(click.exceptions.Exit):
            _check_ffmpeg()


class TestGetVideoDuration:
    def test_returns_duration(self) -> None:
        with patch("analyze_video.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="12.5\n", returncode=0)
            result = _get_video_duration("/usr/bin/ffmpeg", Path("/tmp/video.mp4"))
        assert result == pytest.approx(12.5)
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "/usr/bin/ffprobe"

    def test_returns_zero_on_error(self) -> None:
        with patch("analyze_video.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=1)
            result = _get_video_duration("/usr/bin/ffmpeg", Path("/tmp/video.mp4"))
        assert result == pytest.approx(0.0)


class TestDownloadUrl:
    def test_downloads_file(self, tmp_path: Path) -> None:
        video_file = tmp_path / "input.mp4"

        def fake_curl(*_args: object, **_kwargs: object) -> MagicMock:
            video_file.write_bytes(b"\x00" * 100)
            return MagicMock(returncode=0, stderr="")

        with patch("analyze_video.subprocess.run", side_effect=fake_curl):
            result = _download_url("https://example.com/bug.mp4", tmp_path)
        assert result == video_file
        assert result.exists()

    def test_exits_on_curl_failure(self, tmp_path: Path) -> None:
        with patch("analyze_video.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="Connection refused")
            with pytest.raises(click.exceptions.Exit):
                _download_url("https://example.com/bug.mp4", tmp_path)

    def test_exits_on_empty_file(self, tmp_path: Path) -> None:
        (tmp_path / "input.mp4").write_bytes(b"")

        with patch("analyze_video.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            with pytest.raises(click.exceptions.Exit):
                _download_url("https://example.com/bug.mp4", tmp_path)

    def test_preserves_url_extension(self, tmp_path: Path) -> None:
        video_file = tmp_path / "input.webm"

        def fake_curl(*_args: object, **_kwargs: object) -> MagicMock:
            video_file.write_bytes(b"\x00" * 100)
            return MagicMock(returncode=0, stderr="")

        with patch("analyze_video.subprocess.run", side_effect=fake_curl):
            result = _download_url("https://example.com/demo.webm", tmp_path)
        assert result.suffix == ".webm"
