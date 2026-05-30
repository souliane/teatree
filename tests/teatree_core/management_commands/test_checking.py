"""Tests for the ``t3 <overlay> checking show`` management command (#1529).

The command reads the prior checkpoint, gathers the window, and advances the
marker AFTER gathering — on the default path only. ``--since`` and
``--no-advance`` must NOT move the marker. ``--json`` returns a parseable
payload; the terse path returns the human view. Overlay scoping is read from
``T3_OVERLAY_NAME``. The checkpoint path is pointed at ``tmp_path`` so the
tests never touch the real DATA_DIR.
"""

import json
from datetime import timedelta
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.utils import timezone

from teatree.core.checkpoint import load_checkpoint
from teatree.core.models.merge_clear import ClearRequest, MergeAudit, MergeClear
from teatree.core.models.ticket import Ticket

pytestmark = pytest.mark.django_db

_SHA = "b" * 40


@pytest.fixture
def checkpoint_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "checking_checkpoint_test.json"
    monkeypatch.setattr("teatree.core.checkpoint.checkpoint_path", lambda *_a, **_k: target)
    return target


@pytest.fixture(autouse=True)
def _overlay_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T3_OVERLAY_NAME", "acme")


def _call(*args: str) -> str:
    buf = StringIO()
    call_command(*args, stdout=buf)
    return buf.getvalue()


def _merged_ticket(*, number: int = 42, pr_id: int = 7) -> Ticket:
    ticket = Ticket.objects.create(
        overlay="acme",
        issue_url=f"https://github.com/acme/widgets/issues/{number}",
        state=Ticket.State.IN_REVIEW,
        short_description="widget work",
    )
    clear = MergeClear.issue(
        ClearRequest(
            pr_id=pr_id,
            slug="acme/widgets",
            reviewed_sha=_SHA,
            reviewer_identity="cold-reviewer",
            ticket=ticket,
        ),
    )
    MergeAudit.objects.create(clear=clear, merged_sha=_SHA, required_checks_status="success")
    return ticket


class TestCheckingShow:
    def test_show_returns_terse_text_and_advances_marker(self, checkpoint_file: Path) -> None:
        _merged_ticket()
        out = _call("checking", "show")
        assert "Since " in out
        assert "Merged" in out
        assert "[acme/widgets#7]" in out
        # The default path advances the marker.
        assert checkpoint_file.is_file()
        assert load_checkpoint(checkpoint_file) is not None

    def test_nothing_changed_is_a_single_line(self, checkpoint_file: Path) -> None:
        out = _call("checking", "show").strip()
        assert out.startswith("Nothing since ")
        assert "\n" not in out

    def test_since_does_not_advance_marker(self, checkpoint_file: Path) -> None:
        _merged_ticket()
        since = (timezone.now() - timedelta(hours=6)).isoformat()
        _call("checking", "show", "--since", since)
        assert not checkpoint_file.is_file()

    def test_no_advance_does_not_advance_marker(self, checkpoint_file: Path) -> None:
        _merged_ticket()
        _call("checking", "show", "--no-advance")
        assert not checkpoint_file.is_file()

    def test_json_is_parseable(self, checkpoint_file: Path) -> None:
        _merged_ticket()
        out = _call("checking", "show", "--json")
        payload = json.loads(out)
        assert set(payload) == {"since", "merged", "in_flight", "needs_you", "terse"}
        assert payload["merged"]["items"][0]["label"] == "acme/widgets#7"

    def test_overlay_scoping_excludes_other_overlay(
        self, checkpoint_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _merged_ticket(number=1, pr_id=10)
        other = Ticket.objects.create(
            overlay="other",
            issue_url="https://github.com/other/x/issues/2",
            state=Ticket.State.IN_REVIEW,
        )
        clear = MergeClear.issue(
            ClearRequest(
                pr_id=11,
                slug="other/x",
                reviewed_sha=_SHA,
                reviewer_identity="cold-reviewer",
                ticket=other,
            ),
        )
        MergeAudit.objects.create(clear=clear, merged_sha=_SHA, required_checks_status="success")
        out = _call("checking", "show", "--json")
        payload = json.loads(out)
        labels = [item["label"] for item in payload["merged"]["items"]]
        assert labels == ["acme/widgets#10"]

    def test_second_run_after_advance_reports_nothing(self, checkpoint_file: Path) -> None:
        """Gather-then-advance: an immediate second run sees an empty window."""
        _merged_ticket()
        first = _call("checking", "show")
        assert "Merged" in first
        second = _call("checking", "show").strip()
        assert second.startswith("Nothing since ")
