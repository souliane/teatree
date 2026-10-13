"""The replay window is bounded and half-open."""

import pytest

from teatree.slack_catchup.window import MAX_LOOKBACK_SECONDS, CatchUpWindow


def test_window_clamps_old_evidence_and_excludes_its_upper_edge() -> None:
    now = 1_800_000_000.0
    window = CatchUpWindow.since(now - 10 * 86400, now=now)

    assert window.start == pytest.approx(now - MAX_LOOKBACK_SECONDS - 60)
    assert window.includes(f"{window.start:.6f}")
    assert not window.includes(f"{window.end:.6f}")


def test_missing_evidence_uses_the_same_capped_start() -> None:
    now = 1_800_000_000.0
    assert CatchUpWindow.since(None, now=now).start == CatchUpWindow.since(now - 10 * 86400, now=now).start
