"""Tests for :mod:`teatree.core.checkpoint` — the read-then-advance marker (#1529).

The checkpoint is the durable "when did the user last check?" marker that the
``/t3:checking`` report uses to bound its window. The load-bearing property is
**collapse prevention**: gathering ``[stored, now)`` and only *then* advancing
to ``now`` means an immediate second run sees an empty window — never a window
that the first run already collapsed to empty by advancing too early.

The clock is injected as ``now=`` and the path is pointed at ``tmp_path`` so
the tests stay hermetic — no real DATA_DIR, no network, no git.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from teatree.core.checkpoint import (
    DEFAULT_LOOKBACK,
    advance_checkpoint,
    advance_checkpoint_monotonic,
    checkpoint_path,
    load_checkpoint,
    resolve_window_start,
)


class TestCheckpointPath:
    def test_keyed_by_explicit_overlay(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("teatree.core.checkpoint.DATA_DIR", tmp_path)
        path = checkpoint_path(overlay="acme")
        assert path == tmp_path / "checking_checkpoint_acme.json"

    def test_keyed_by_env_overlay(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("teatree.core.checkpoint.DATA_DIR", tmp_path)
        monkeypatch.setenv("T3_OVERLAY_NAME", "widgets")
        assert checkpoint_path() == tmp_path / "checking_checkpoint_widgets.json"

    def test_empty_overlay_falls_back_to_global(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("teatree.core.checkpoint.DATA_DIR", tmp_path)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        assert checkpoint_path() == tmp_path / "checking_checkpoint_global.json"


class TestAdvanceAndLoadRoundTrip:
    def test_round_trip_is_tz_aware(self, tmp_path: Path) -> None:
        path = tmp_path / "cp.json"
        now = datetime(2026, 5, 30, 12, 0, tzinfo=UTC)
        advance_checkpoint(now, path)
        loaded = load_checkpoint(path)
        assert loaded == now
        assert loaded is not None
        assert loaded.tzinfo is not None

    def test_naive_now_is_coerced_to_utc(self, tmp_path: Path) -> None:
        path = tmp_path / "cp.json"
        advance_checkpoint(datetime(2026, 5, 30, 12, 0), path)  # noqa: DTZ001 — exercising the coercion path
        loaded = load_checkpoint(path)
        assert loaded == datetime(2026, 5, 30, 12, 0, tzinfo=UTC)

    def test_advance_overwrites_prior_marker(self, tmp_path: Path) -> None:
        path = tmp_path / "cp.json"
        first = datetime(2026, 5, 30, 9, 0, tzinfo=UTC)
        second = datetime(2026, 5, 30, 15, 0, tzinfo=UTC)
        advance_checkpoint(first, path)
        advance_checkpoint(second, path)
        assert load_checkpoint(path) == second


class TestLoadTolerance:
    def test_missing_file_is_none(self, tmp_path: Path) -> None:
        assert load_checkpoint(tmp_path / "absent.json") is None

    def test_corrupt_json_is_none(self, tmp_path: Path) -> None:
        path = tmp_path / "cp.json"
        path.write_text("{not json", encoding="utf-8")
        assert load_checkpoint(path) is None

    def test_half_written_payload_is_none(self, tmp_path: Path) -> None:
        path = tmp_path / "cp.json"
        path.write_text('{"last_checked_at": ', encoding="utf-8")
        assert load_checkpoint(path) is None

    def test_non_dict_payload_is_none(self, tmp_path: Path) -> None:
        path = tmp_path / "cp.json"
        path.write_text('"just a string"', encoding="utf-8")
        assert load_checkpoint(path) is None

    def test_unparseable_timestamp_is_none(self, tmp_path: Path) -> None:
        path = tmp_path / "cp.json"
        path.write_text('{"last_checked_at": "not-a-date"}', encoding="utf-8")
        assert load_checkpoint(path) is None


class TestResolveWindowStart:
    def test_explicit_since_wins_over_checkpoint(self, tmp_path: Path) -> None:
        path = tmp_path / "cp.json"
        advance_checkpoint(datetime(2026, 5, 30, 9, 0, tzinfo=UTC), path)
        now = datetime(2026, 5, 30, 18, 0, tzinfo=UTC)
        start = resolve_window_start(since="2026-05-29T00:00:00+00:00", now=now, path=path)
        assert start == datetime(2026, 5, 29, 0, 0, tzinfo=UTC)

    def test_naive_since_coerced_to_utc(self, tmp_path: Path) -> None:
        now = datetime(2026, 5, 30, 18, 0, tzinfo=UTC)
        start = resolve_window_start(since="2026-05-29T08:00:00", now=now, path=tmp_path / "cp.json")
        assert start == datetime(2026, 5, 29, 8, 0, tzinfo=UTC)

    def test_unparseable_since_raises_friendly_value_error(self, tmp_path: Path) -> None:
        # #1652: a non-ISO --since (e.g. "yesterday") raises a typed,
        # message-bearing ValueError naming the expected format — not a raw
        # datetime.fromisoformat ValueError the command would dump as a
        # traceback.
        now = datetime(2026, 5, 30, 18, 0, tzinfo=UTC)
        with pytest.raises(ValueError, match="ISO-8601") as exc_info:
            resolve_window_start(since="yesterday", now=now, path=tmp_path / "cp.json")
        assert "yesterday" in str(exc_info.value)

    def test_checkpoint_used_when_no_since(self, tmp_path: Path) -> None:
        path = tmp_path / "cp.json"
        stored = datetime(2026, 5, 30, 9, 0, tzinfo=UTC)
        advance_checkpoint(stored, path)
        now = datetime(2026, 5, 30, 18, 0, tzinfo=UTC)
        assert resolve_window_start(since="", now=now, path=path) == stored

    def test_default_lookback_when_no_since_and_no_checkpoint(self, tmp_path: Path) -> None:
        now = datetime(2026, 5, 30, 18, 0, tzinfo=UTC)
        start = resolve_window_start(since="", now=now, path=tmp_path / "cp.json")
        assert start == now - DEFAULT_LOOKBACK

    def test_future_since_falls_back_to_default_lookback(self, tmp_path: Path) -> None:
        # A future explicit --since would yield an empty [future, now) window;
        # the guard restores the default lookback so the report is never empty.
        now = datetime(2026, 5, 30, 18, 0, tzinfo=UTC)
        start = resolve_window_start(since="2026-06-01T00:00:00+00:00", now=now, path=tmp_path / "cp.json")
        assert start == now - DEFAULT_LOOKBACK

    def test_since_equal_to_now_falls_back(self, tmp_path: Path) -> None:
        # A start exactly at now collapses [now, now) to empty — also clamped.
        now = datetime(2026, 5, 30, 18, 0, tzinfo=UTC)
        start = resolve_window_start(since=now.isoformat(), now=now, path=tmp_path / "cp.json")
        assert start == now - DEFAULT_LOOKBACK

    def test_future_checkpoint_falls_back_to_default_lookback(self, tmp_path: Path) -> None:
        # A clock-skewed checkpoint written ahead of now must not yield an empty
        # window that then advances the marker forward, silently skipping events.
        path = tmp_path / "cp.json"
        now = datetime(2026, 5, 30, 18, 0, tzinfo=UTC)
        advance_checkpoint(now + timedelta(hours=6), path)
        start = resolve_window_start(since="", now=now, path=path)
        assert start == now - DEFAULT_LOOKBACK


class TestMonotonicAdvance:
    def test_advances_forward_when_now_is_later(self, tmp_path: Path) -> None:
        path = tmp_path / "cp.json"
        advance_checkpoint(datetime(2026, 5, 30, 9, 0, tzinfo=UTC), path)
        later = datetime(2026, 5, 30, 15, 0, tzinfo=UTC)
        advance_checkpoint_monotonic(later, path)
        assert load_checkpoint(path) == later

    def test_does_not_move_marker_backward(self, tmp_path: Path) -> None:
        # A clock regression (now earlier than the stored marker) must NOT
        # rewind the marker — rewinding would mark already-seen events unseen.
        path = tmp_path / "cp.json"
        stored = datetime(2026, 5, 30, 15, 0, tzinfo=UTC)
        advance_checkpoint(stored, path)
        earlier = datetime(2026, 5, 30, 9, 0, tzinfo=UTC)
        advance_checkpoint_monotonic(earlier, path)
        assert load_checkpoint(path) == stored

    def test_keeps_future_marker_untouched(self, tmp_path: Path) -> None:
        path = tmp_path / "cp.json"
        future = datetime(2026, 5, 30, 23, 0, tzinfo=UTC)
        advance_checkpoint(future, path)
        advance_checkpoint_monotonic(datetime(2026, 5, 30, 18, 0, tzinfo=UTC), path)
        assert load_checkpoint(path) == future

    def test_writes_when_no_existing_marker(self, tmp_path: Path) -> None:
        path = tmp_path / "cp.json"
        now = datetime(2026, 5, 30, 18, 0, tzinfo=UTC)
        advance_checkpoint_monotonic(now, path)
        assert load_checkpoint(path) == now


class TestFutureWindowGuardEndToEnd:
    def test_future_checkpoint_does_not_collapse_then_advance(self, tmp_path: Path) -> None:
        """A future marker must not produce an empty window that then advances.

        The default command path: resolve the start, gather, then advance
        monotonically. A future/skewed checkpoint must (a) resolve to a real
        non-empty window via the default lookback, and (b) leave the marker
        untouched by the monotonic advance — so no real event is ever skipped.
        """
        path = tmp_path / "cp.json"
        now = datetime(2026, 5, 30, 18, 0, tzinfo=UTC)
        future_marker = now + timedelta(hours=6)
        advance_checkpoint(future_marker, path)

        start = resolve_window_start(since="", now=now, path=path)
        assert start == now - DEFAULT_LOOKBACK  # non-empty real window
        assert start < now

        advance_checkpoint_monotonic(now, path)
        # The future marker is preserved (never rewound), so the next run still
        # bounds correctly and no events between were silently skipped.
        assert load_checkpoint(path) == future_marker


class TestCollapsePreventionProperty:
    def test_gather_then_advance_makes_second_run_empty(self, tmp_path: Path) -> None:
        """Read ``[stored, now)`` THEN advance — an immediate second run is empty.

        This is the structural guarantee: advancing only after gathering
        means the window the first run reported is exactly ``[prior, now)``,
        and the second run's window ``[now, now2)`` excludes everything the
        first already covered. Advancing before gathering would collapse the
        first window to empty.
        """
        path = tmp_path / "cp.json"
        prior = datetime(2026, 5, 30, 8, 0, tzinfo=UTC)
        advance_checkpoint(prior, path)

        first_now = datetime(2026, 5, 30, 12, 0, tzinfo=UTC)
        first_start = resolve_window_start(since="", now=first_now, path=path)
        assert first_start == prior  # the first run sees a non-empty window
        advance_checkpoint(first_now, path)  # advance AFTER gathering

        second_now = first_now + timedelta(seconds=1)
        second_start = resolve_window_start(since="", now=second_now, path=path)
        assert second_start == first_now
        # The second window [first_now, second_now) excludes the first window.
        assert second_start >= first_start
