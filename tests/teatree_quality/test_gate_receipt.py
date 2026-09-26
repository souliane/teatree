"""A missing or stale local gate run is visible in the PR description."""

import json
from unittest.mock import patch

from teatree.quality.gate_receipt import append_gate_notice, write_gate_receipt


def test_missing_receipt_marks_pr_incomplete(tmp_path) -> None:
    with patch("teatree.quality.gate_receipt._receipt_path", return_value=tmp_path / "receipt.json"):
        assert "INCOMPLETE" in append_gate_notice("Description", tmp_path)


def test_green_receipt_for_current_clean_head_marks_pr_verified(tmp_path) -> None:
    receipt = tmp_path / "receipt.json"
    with (
        patch("teatree.quality.gate_receipt._receipt_path", return_value=receipt),
        patch("teatree.quality.gate_receipt._snapshot", return_value=("abc123", True)),
    ):
        write_gate_receipt(tmp_path, state="green", reason="both stages passed")
        description = append_gate_notice("Description", tmp_path)
    assert "green" in description
    assert "INCOMPLETE" not in description
    assert json.loads(receipt.read_text())["head"] == "abc123"


def test_dirty_or_changed_head_cannot_reuse_old_green(tmp_path) -> None:
    receipt = tmp_path / "receipt.json"
    with (
        patch("teatree.quality.gate_receipt._receipt_path", return_value=receipt),
        patch("teatree.quality.gate_receipt._snapshot", side_effect=[("abc123", True), ("def456", True)]),
    ):
        write_gate_receipt(tmp_path, state="green", reason="both stages passed")
        assert "INCOMPLETE" in append_gate_notice("Description", tmp_path)


def test_refusal_receipt_carries_safe_reason_into_pr(tmp_path) -> None:
    with (
        patch("teatree.quality.gate_receipt._receipt_path", return_value=tmp_path / "receipt.json"),
        patch("teatree.quality.gate_receipt._snapshot", return_value=("abc123", True)),
    ):
        write_gate_receipt(tmp_path, state="incomplete", reason="disk below floor")
        description = append_gate_notice("Description", tmp_path)
    assert "INCOMPLETE" in description
    assert "disk below floor" in description
