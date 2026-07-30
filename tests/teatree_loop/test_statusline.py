from pathlib import Path

import pytest

from teatree.loop.statusline import StatuslineEntry, StatuslineZones, default_path, render, statusline_for_slack


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
        # The "Action needed:" / "In flight:" zone headers were removed —
        # color carries the signal, no separate label is rendered.
        assert "Action needed:" not in content
        assert "In flight:" not in content
        assert "legend:" not in content

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

        # Light-gray 256-color for anchors (replaces legacy dim `\033[2;37m`,
        # which is unreadable on dark themes).
        assert "\033[38;5;244m" in content  # dim/gray for anchors
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


class TestStatuslineForSlack:
    """``statusline_for_slack`` transforms on-disk statusline → Slack mrkdwn (#1121)."""

    def test_statusline_for_slack_strips_ansi_and_converts_osc8(self, tmp_path: Path) -> None:
        target = tmp_path / "statusline.txt"
        entry = StatuslineEntry(
            text="PR !123",
            url="https://gitlab.com/x/y/-/merge_requests/123",
        )
        zones = StatuslineZones(action_needed=[entry])
        render(zones, target=target, colorize=True)

        out = statusline_for_slack(path=target)

        # ANSI CSI escapes (color/reset) removed.
        assert "\033[" not in out
        # OSC 8 hyperlink wrappers gone, replaced by Slack mrkdwn link.
        assert "\033]8" not in out
        assert "<https://gitlab.com/x/y/-/merge_requests/123|PR !123>" in out

    def test_statusline_for_slack_returns_empty_when_file_missing(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist.txt"
        assert statusline_for_slack(path=missing) == ""

    def test_statusline_for_slack_returns_empty_when_file_empty(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.txt"
        empty.write_text("")
        assert statusline_for_slack(path=empty) == ""

    def test_statusline_for_slack_preserves_plain_text_lines(self, tmp_path: Path) -> None:
        target = tmp_path / "statusline.txt"
        zones = StatuslineZones(
            anchors=["overlay=teatree", "ticket=#1121"],
            in_flight=["sweep_my_prs (3 prs)"],
        )
        render(zones, target=target, colorize=True)

        out = statusline_for_slack(path=target)

        assert "overlay=teatree" in out
        assert "ticket=#1121" in out
        assert "sweep_my_prs (3 prs)" in out
        # No ANSI noise from coloured lines.
        assert "\033" not in out

    def test_statusline_for_slack_default_path_when_unspecified(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        zones = StatuslineZones(anchors=["overlay=teatree"])
        render(zones, colorize=True)

        out = statusline_for_slack()

        assert "overlay=teatree" in out
        assert "\033" not in out


class TestLoopOwnerAnchor:
    """``loop_owner_anchor`` zone+text mapping (#1073, #1156).

    #1156 narrowed this helper to only emit the foreign-hijack RED
    line. The dim ``t3-master=THIS session ✓`` /
    ``t3-master=unclaimed`` lines were replaced by
    :func:`live_loops_anchor` which renders one line per live
    :class:`LoopLease` row.
    """

    def _status(self, *, owner: str, is_live: bool, driver: str = "self_pump"):
        from teatree.core.managers import OwnershipStatus  # noqa: PLC0415

        return OwnershipStatus(owner_session=owner, expires_at=None, is_live=is_live, driver=driver)

    def test_this_session_owns_with_driver_returns_blank(self) -> None:
        from teatree.loop.statusline import loop_owner_anchor  # noqa: PLC0415

        zone, line = loop_owner_anchor(self._status(owner="sess-A", is_live=True), "sess-A")
        assert zone == "anchors"
        # No verbose ``THIS session ✓`` line — :func:`live_loops_anchor` owns
        # the dim line now.
        assert line == ""

    def test_this_session_owns_but_driverless_is_red(self) -> None:
        from teatree.loop.statusline import loop_owner_anchor  # noqa: PLC0415 — deferred: test-local import

        zone, line = loop_owner_anchor(self._status(owner="sess-A", is_live=True, driver=""), "sess-A")
        assert zone == "action_needed"
        assert line == "t3-master=this session · DRIVERLESS"

    def test_different_live_owner_is_red_action_needed(self) -> None:
        from teatree.loop.statusline import loop_owner_anchor  # noqa: PLC0415

        zone, line = loop_owner_anchor(self._status(owner="abcdef0123456789", is_live=True), "sess-A")
        assert zone == "action_needed"
        assert line == "t3-master=session abcdef01 (NOT this session)"

    def test_no_live_owner_returns_blank(self) -> None:
        from teatree.loop.statusline import loop_owner_anchor  # noqa: PLC0415

        zone, line = loop_owner_anchor(self._status(owner="", is_live=False), "sess-A")
        assert zone == "anchors"
        # No verbose ``unclaimed`` line — absent lease ≡ no live-loop row.
        assert line == ""

    def test_anonymous_session_with_live_owner_is_red(self) -> None:
        from teatree.loop.statusline import loop_owner_anchor  # noqa: PLC0415

        zone, line = loop_owner_anchor(self._status(owner="ownersess", is_live=True), "")
        assert zone == "action_needed"
        assert line == "t3-master=session ownerses (NOT this session)"
