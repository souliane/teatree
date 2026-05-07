from pathlib import Path

import pytest

from teatree.loop.statusline import StatuslineEntry, StatuslineZones, default_path, render


class TestStatuslineRender:
    def test_writes_three_labeled_zones(self, tmp_path: Path) -> None:
        target = tmp_path / "statusline.txt"
        zones = StatuslineZones(
            anchors=["overlay=teatree", "ticket=#541", "branch=ac-teatree-541-ticket"],
            action_needed=["PR #1234 pipeline failed"],
            in_flight=["sweep_my_prs (3 prs)"],
        )

        render(zones, target=target)

        content = target.read_text()
        assert "overlay=teatree" in content
        assert "ticket=#541" in content
        assert "PR #1234 pipeline failed" in content
        assert "sweep_my_prs (3 prs)" in content

    def test_zone_order_is_anchors_action_inflight(self, tmp_path: Path) -> None:
        target = tmp_path / "statusline.txt"
        zones = StatuslineZones(
            anchors=["A"],
            action_needed=["B"],
            in_flight=["C"],
        )

        render(zones, target=target)

        content = target.read_text()
        assert content.index("A") < content.index("B") < content.index("C")

    def test_omits_empty_zones(self, tmp_path: Path) -> None:
        target = tmp_path / "statusline.txt"
        zones = StatuslineZones(anchors=["overlay=teatree"], action_needed=[], in_flight=[])

        render(zones, target=target)

        content = target.read_text()
        assert "overlay=teatree" in content
        assert "Action" not in content
        assert "In flight" not in content

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "deep" / "nested" / "statusline.txt"
        zones = StatuslineZones(anchors=["x"], action_needed=[], in_flight=[])

        render(zones, target=target, colorize=False)

        assert target.exists()
        assert target.read_text().strip() == "x"

    def test_overwrites_previous_content(self, tmp_path: Path) -> None:
        target = tmp_path / "statusline.txt"
        target.write_text("stale content from previous tick\n")

        zones = StatuslineZones(anchors=["fresh"], action_needed=[], in_flight=[])
        render(zones, target=target)

        assert "stale" not in target.read_text()
        assert "fresh" in target.read_text()

    def test_atomic_write_no_partial_reads(self, tmp_path: Path) -> None:
        target = tmp_path / "statusline.txt"
        zones = StatuslineZones(
            anchors=["a"] * 100,
            action_needed=["b"] * 100,
            in_flight=["c"] * 100,
        )

        render(zones, target=target)

        # File exists fully populated; no .tmp leftover.
        assert target.exists()
        assert not list(tmp_path.glob("*.tmp"))

    def test_render_cleans_up_tmp_file_on_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """If the rename fails, the temp file is removed."""
        target = tmp_path / "statusline.txt"
        zones = StatuslineZones(anchors=["x"], action_needed=[], in_flight=[])

        original_replace = Path.replace

        def fail_replace(self: Path, *args: object, **kwargs: object) -> None:
            msg = "replace failed"
            raise OSError(msg)

        monkeypatch.setattr(Path, "replace", fail_replace)
        try:
            with pytest.raises(OSError, match="replace failed"):
                render(zones, target=target)
        finally:
            monkeypatch.setattr(Path, "replace", original_replace)

        # Tmp file should be cleaned up.
        assert not list(tmp_path.glob("*.tmp"))


class TestStatuslineEntry:
    def test_url_renders_as_osc8_hyperlink(self, tmp_path: Path) -> None:
        target = tmp_path / "statusline.txt"
        entry = StatuslineEntry(text="PR #545: feat(loop)", url="https://github.com/owner/repo/pull/545")
        zones = StatuslineZones(in_flight=[entry])

        render(zones, target=target, colorize=True)
        content = target.read_text()

        assert "\033]8;;https://github.com/owner/repo/pull/545\033\\" in content
        assert "PR #545: feat(loop)" in content
        assert content.endswith("\n")

    def test_no_color_falls_back_to_text_url(self, tmp_path: Path) -> None:
        target = tmp_path / "statusline.txt"
        entry = StatuslineEntry(text="PR #545", url="https://example.com/pr/545")
        zones = StatuslineZones(in_flight=[entry])

        render(zones, target=target, colorize=False)
        content = target.read_text()

        assert "\033]" not in content  # no OSC sequences
        assert "PR #545 <https://example.com/pr/545>" in content


class TestStatuslineColors:
    def test_zone_specific_ansi_colors_when_colorize_true(self, tmp_path: Path) -> None:
        target = tmp_path / "statusline.txt"
        zones = StatuslineZones(
            anchors=["tick @ 12:00"],
            action_needed=["PR #1 failed"],
            in_flight=["PR #2 open"],
        )

        render(zones, target=target, colorize=True)
        content = target.read_text()

        assert "\033[2;37m" in content  # dim for anchors
        assert "\033[1;31m" in content  # red for action_needed
        assert "\033[1;36m" in content  # cyan for in_flight
        assert "\033[0m" in content  # reset after each line

    def test_no_color_env_strips_ansi(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NO_COLOR", "1")
        target = tmp_path / "statusline.txt"
        zones = StatuslineZones(anchors=["tick"], action_needed=["x"], in_flight=["y"])

        render(zones, target=target)
        content = target.read_text()

        assert "\033" not in content


class TestDefaultPath:
    def test_uses_xdg_data_home_when_set(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        assert default_path() == tmp_path / "teatree" / "statusline.txt"

    def test_falls_back_to_home_local_share(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert default_path() == tmp_path / ".local" / "share" / "teatree" / "statusline.txt"
