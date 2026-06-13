"""Tests for ``teatree.loop.admit_budget`` — the orchestrate admit-budget sidecar (#1796).

The reconciled fan-out (#1796) makes ``orchestrate_phase`` a read-only budget
PLANNER: the tick persists an admit-budget *ceiling* next to ``tick-meta.json``
and the live ``claim_next`` reads it before its CAS. This module owns the
write/read of that sidecar key.

The reader has a ``written_at`` TTL (~2x the tick cadence) and **fails open to
UNCLAMPED** (returns ``None``) when the budget is absent, stale, or corrupt — a
dead loop must never wrongly clamp live dispatch.
"""

import json
import time
from pathlib import Path
from unittest.mock import patch

from teatree.loop.admit_budget import BUDGET_KEY, clear_admit_budget, read_admit_budget, write_admit_budget


class TestAdmitBudgetRoundTrip:
    def test_write_then_read_returns_the_budget(self, tmp_path: Path) -> None:
        sl = tmp_path / "statusline.txt"
        write_admit_budget(3, statusline_path=sl)
        assert read_admit_budget(statusline_path=sl, cadence_seconds=720) == 3

    def test_write_zero_budget_is_readable_as_zero(self, tmp_path: Path) -> None:
        # Zero is a real clamp (claim nothing), distinct from absence (unclamped).
        sl = tmp_path / "statusline.txt"
        write_admit_budget(0, statusline_path=sl)
        assert read_admit_budget(statusline_path=sl, cadence_seconds=720) == 0

    def test_write_creates_the_sidecar_beside_the_statusline(self, tmp_path: Path) -> None:
        sl = tmp_path / "nested" / "statusline.txt"
        write_admit_budget(2, statusline_path=sl)
        meta = sl.with_name("tick-meta.json")
        assert meta.is_file()
        payload = json.loads(meta.read_text(encoding="utf-8"))
        assert payload[BUDGET_KEY] == 2

    def test_write_preserves_existing_tick_meta_keys(self, tmp_path: Path) -> None:
        sl = tmp_path / "statusline.txt"
        meta = sl.with_name("tick-meta.json")
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text(json.dumps({"next_epoch": 999, "cadence": 720}) + "\n", encoding="utf-8")
        write_admit_budget(1, statusline_path=sl)
        payload = json.loads(meta.read_text(encoding="utf-8"))
        assert payload["next_epoch"] == 999  # untouched
        assert payload[BUDGET_KEY] == 1


class TestAdmitBudgetClear:
    def test_clear_removes_the_budget_key_leaving_other_keys(self, tmp_path: Path) -> None:
        sl = tmp_path / "statusline.txt"
        write_admit_budget(4, statusline_path=sl)
        # Pre-seed an unrelated key to prove clear is surgical.
        meta = sl.with_name("tick-meta.json")
        payload = json.loads(meta.read_text(encoding="utf-8"))
        payload["next_epoch"] = 7
        meta.write_text(json.dumps(payload) + "\n", encoding="utf-8")

        clear_admit_budget(statusline_path=sl)
        after = json.loads(meta.read_text(encoding="utf-8"))
        assert BUDGET_KEY not in after
        assert after["next_epoch"] == 7
        assert read_admit_budget(statusline_path=sl, cadence_seconds=720) is None

    def test_clear_when_no_sidecar_exists_is_a_noop(self, tmp_path: Path) -> None:
        sl = tmp_path / "statusline.txt"
        clear_admit_budget(statusline_path=sl)  # must not raise
        assert read_admit_budget(statusline_path=sl, cadence_seconds=720) is None

    def test_clear_swallows_a_write_error_fail_open(self, tmp_path: Path) -> None:
        # A read-only filesystem (write_text raises OSError) must not crash the
        # tick — clear fails open, leaving the (stale) sidecar to the reader's TTL.
        sl = tmp_path / "statusline.txt"
        write_admit_budget(3, statusline_path=sl)
        with patch.object(Path, "write_text", side_effect=OSError("read-only fs")):
            clear_admit_budget(statusline_path=sl)  # must not raise


class TestAdmitBudgetFailOpen:
    def test_absent_sidecar_reads_none_unclamped(self, tmp_path: Path) -> None:
        sl = tmp_path / "statusline.txt"
        assert read_admit_budget(statusline_path=sl, cadence_seconds=720) is None

    def test_absent_budget_key_reads_none_unclamped(self, tmp_path: Path) -> None:
        sl = tmp_path / "statusline.txt"
        meta = sl.with_name("tick-meta.json")
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text(json.dumps({"next_epoch": 1, "cadence": 720}) + "\n", encoding="utf-8")
        assert read_admit_budget(statusline_path=sl, cadence_seconds=720) is None

    def test_corrupt_sidecar_reads_none_unclamped(self, tmp_path: Path) -> None:
        sl = tmp_path / "statusline.txt"
        meta = sl.with_name("tick-meta.json")
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text("not json {{{", encoding="utf-8")
        assert read_admit_budget(statusline_path=sl, cadence_seconds=720) is None

    def test_non_int_budget_value_reads_none_unclamped(self, tmp_path: Path) -> None:
        sl = tmp_path / "statusline.txt"
        meta = sl.with_name("tick-meta.json")
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text(
            json.dumps({BUDGET_KEY: "three", f"{BUDGET_KEY}_written_at": time.time()}) + "\n",
            encoding="utf-8",
        )
        assert read_admit_budget(statusline_path=sl, cadence_seconds=720) is None

    def test_stale_budget_past_ttl_is_ignored_unclamped(self, tmp_path: Path) -> None:
        # written_at older than ~2x cadence → a dead loop wrote it → ignore it,
        # fail open to UNCLAMPED so live dispatch is never wrongly throttled.
        sl = tmp_path / "statusline.txt"
        meta = sl.with_name("tick-meta.json")
        meta.parent.mkdir(parents=True, exist_ok=True)
        cadence = 720
        stale_at = time.time() - (2 * cadence + 60)
        meta.write_text(
            json.dumps({BUDGET_KEY: 1, f"{BUDGET_KEY}_written_at": stale_at}) + "\n",
            encoding="utf-8",
        )
        assert read_admit_budget(statusline_path=sl, cadence_seconds=cadence) is None

    def test_fresh_budget_within_ttl_is_honoured(self, tmp_path: Path) -> None:
        sl = tmp_path / "statusline.txt"
        meta = sl.with_name("tick-meta.json")
        meta.parent.mkdir(parents=True, exist_ok=True)
        cadence = 720
        fresh_at = time.time() - 10
        meta.write_text(
            json.dumps({BUDGET_KEY: 2, f"{BUDGET_KEY}_written_at": fresh_at}) + "\n",
            encoding="utf-8",
        )
        assert read_admit_budget(statusline_path=sl, cadence_seconds=cadence) == 2

    def test_missing_written_at_is_treated_as_stale_unclamped(self, tmp_path: Path) -> None:
        # A budget with no timestamp cannot be proven fresh → fail open.
        sl = tmp_path / "statusline.txt"
        meta = sl.with_name("tick-meta.json")
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text(json.dumps({BUDGET_KEY: 5}) + "\n", encoding="utf-8")
        assert read_admit_budget(statusline_path=sl, cadence_seconds=720) is None
