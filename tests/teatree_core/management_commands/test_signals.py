"""Tests for the ``t3 <overlay> signals`` management command (SIG-PR-1)."""

import json
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from tests.factories import MergeAuditFactory, MergeClearFactory

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _call(*args: str, **kwargs: object) -> str:
    buf = StringIO()
    call_command("signals", *args, stdout=buf, **kwargs)
    return buf.getvalue()


class TestSignalsCommand:
    def test_json_emits_five_signals_and_verdict(self) -> None:
        payload = json.loads(_call(json_output=True))
        assert payload["window_days"] == 28
        assert payload["verdict"] in {"ok", "regressing", "red"}
        assert len(payload["signals"]) == 5
        assert {row["provider_id"] for row in payload["signals"]} == {
            "first_try_green",
            "defect_escape",
            "review_catch",
            "merge_latency",
            "repair_burn",
        }

    def test_window_days_flows_through(self) -> None:
        payload = json.loads(_call("--window-days", "7", json_output=True))
        assert payload["window_days"] == 7

    def test_human_view_renders_markdown_table(self) -> None:
        out = _call()
        assert "Factory signals" in out
        assert "first_try_green" in out

    def test_rubber_stamp_window_surfaces_red(self) -> None:
        now = timezone.now()
        for i in range(5):
            merged_at = now - timedelta(days=5)
            clear = MergeClearFactory(
                pr_id=901 + i,
                issued_at=merged_at - timedelta(hours=1),
                consumed_at=merged_at,
            )
            MergeAuditFactory(clear=clear, merged_at=merged_at)
        payload = json.loads(_call(json_output=True))
        assert payload["verdict"] == "red"
        review = next(row for row in payload["signals"] if row["provider_id"] == "review_catch")
        assert review["tripped"] is True
