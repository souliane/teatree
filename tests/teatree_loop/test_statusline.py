from pathlib import Path

import pytest

from teatree.loop.statusline import StatuslineZones, default_path, render


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

        render(zones, target=target)

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


class TestDefaultPath:
    def test_uses_xdg_data_home_when_set(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        assert default_path() == tmp_path / "teatree" / "statusline.txt"

    def test_falls_back_to_home_local_share(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert default_path() == tmp_path / ".local" / "share" / "teatree" / "statusline.txt"
