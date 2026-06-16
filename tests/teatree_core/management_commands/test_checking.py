"""Tests for the ``t3 <overlay> checking show`` management command (#1529).

The command reads the prior checkpoint, gathers the window, and advances the
marker AFTER gathering — on the default path only. ``--since`` and
``--no-advance`` must NOT move the marker. ``--json`` returns a parseable
payload; the terse path returns the human view. Overlay scoping is read from
``T3_OVERLAY_NAME``. The checkpoint path is pointed at ``tmp_path`` so the
tests never touch the real DATA_DIR.
"""

import json
from contextlib import AbstractContextManager
from datetime import timedelta
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.utils import timezone

import teatree.core.overlay_loader as overlay_loader_mod
from teatree.core.checkpoint import advance_checkpoint, load_checkpoint
from teatree.core.models.merge_clear import ClearRequest, MergeAudit, MergeClear
from teatree.core.models.ticket import Ticket
from teatree.core.overlay import OverlayBase, OverlayMetadata

# ast-grep-ignore: ac-django-no-pytest-django-db
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


# ---------------------------------------------------------------------------
# Synthetic overlay doubles for multi-overlay tests
# ---------------------------------------------------------------------------


class _MinimalMeta(OverlayMetadata):
    def get_tool_commands(self):
        return []


class _AcmeOverlay(OverlayBase):
    metadata = _MinimalMeta()

    def get_repos(self) -> list[str]:
        return ["acme/widgets"]

    def get_provision_steps(self, worktree):
        return []

    def get_run_commands(self, worktree):
        return {}

    def get_test_command(self, worktree):
        return []


class _BetaOverlay(OverlayBase):
    metadata = _MinimalMeta()

    def get_repos(self) -> list[str]:
        return ["beta/core"]

    def get_provision_steps(self, worktree):
        return []

    def get_run_commands(self, worktree):
        return {}

    def get_test_command(self, worktree):
        return []


def _two_overlay_patch() -> AbstractContextManager[Any]:
    """Patch _discover_overlays to return two synthetic overlays."""
    result: dict[str, OverlayBase] = {"acme": _AcmeOverlay(), "beta": _BetaOverlay()}

    def _fake_discover() -> dict[str, OverlayBase]:
        return result

    _fake_discover.cache_clear = lambda: None

    return patch.object(overlay_loader_mod, "_discover_overlays", new=_fake_discover)


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
    """Single-overlay path via ``--this-overlay`` (the old default behavior)."""

    def test_show_returns_terse_text_and_advances_marker(self, checkpoint_file: Path) -> None:
        _merged_ticket()
        out = _call("checking", "show", "--this-overlay")
        assert "Since " in out
        assert "Merged" in out
        assert "[acme/widgets#7]" in out
        # The default path advances the marker.
        assert checkpoint_file.is_file()
        assert load_checkpoint(checkpoint_file) is not None

    def test_nothing_changed_is_a_single_line(self, checkpoint_file: Path) -> None:
        out = _call("checking", "show", "--this-overlay").strip()
        assert out.startswith("Nothing since ")
        assert "\n" not in out

    def test_since_does_not_advance_marker(self, checkpoint_file: Path) -> None:
        _merged_ticket()
        since = (timezone.now() - timedelta(hours=6)).isoformat()
        _call("checking", "show", "--this-overlay", "--since", since)
        assert not checkpoint_file.is_file()

    def test_no_advance_does_not_advance_marker(self, checkpoint_file: Path) -> None:
        _merged_ticket()
        _call("checking", "show", "--this-overlay", "--no-advance")
        assert not checkpoint_file.is_file()

    def test_unparseable_since_exits_nonzero_not_traceback(self, checkpoint_file: Path) -> None:
        # #1652: --since yesterday is rejected with a clean non-zero exit
        # (typer.BadParameter), never a raw datetime ValueError traceback,
        # and the marker is left untouched.
        from typer.testing import CliRunner  # noqa: PLC0415

        from teatree.core.management.commands.checking import Command  # noqa: PLC0415

        result = CliRunner().invoke(Command().typer_app, ["show", "--this-overlay", "--since", "yesterday"])
        assert result.exit_code != 0
        assert not checkpoint_file.is_file()

    def test_json_is_parseable(self, checkpoint_file: Path) -> None:
        _merged_ticket()
        out = _call("checking", "show", "--this-overlay", "--json")
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
        out = _call("checking", "show", "--this-overlay", "--json")
        payload = json.loads(out)
        labels = [item["label"] for item in payload["merged"]["items"]]
        assert labels == ["acme/widgets#10"]

    def test_null_ticket_ceremony_merge_scoped_by_resolved_repo(
        self, checkpoint_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A NULL-ticket ceremony merge surfaces when its repo is the overlay's (#1559).

        Runs against the real bundled ``t3-teatree`` overlay so the command's
        own ``_resolve_overlay_repos`` derives the repo set (``souliane/teatree``)
        — the ticket-FK JOIN that previously dropped every NULL-ticket CLEAR is
        gone, while a foreign-repo merge stays excluded.
        """
        monkeypatch.setenv("T3_OVERLAY_NAME", "t3-teatree")
        for pr_id, slug in ((50, "souliane/teatree"), (51, "other-org/other-repo")):
            clear = MergeClear.objects.create(
                ticket=None,
                pr_id=pr_id,
                slug=slug,
                reviewed_sha=_SHA,
                reviewer_identity="cold-reviewer",
                gh_verify_result=MergeClear.VerifyResult.GREEN,
                blast_class=MergeClear.BlastClass.LOGIC,
            )
            MergeAudit.objects.create(clear=clear, merged_sha=_SHA, required_checks_status="success")
        out = _call("checking", "show", "--this-overlay", "--json")
        labels = [item["label"] for item in json.loads(out)["merged"]["items"]]
        assert labels == ["souliane/teatree#50"]

    def test_second_run_after_advance_reports_nothing(self, checkpoint_file: Path) -> None:
        """Gather-then-advance: an immediate second run sees an empty window."""
        _merged_ticket()
        first = _call("checking", "show", "--this-overlay")
        assert "Merged" in first
        second = _call("checking", "show", "--this-overlay").strip()
        assert second.startswith("Nothing since ")

    def test_future_checkpoint_does_not_collapse_window_or_advance_backward(self, checkpoint_file: Path) -> None:
        """A skewed future marker must still report recent events, not collapse.

        A checkpoint written ahead of the clock would yield an empty
        ``[future, now)`` window and then advance the marker forward, silently
        skipping the real events. The guard restores the default lookback so the
        recent merge surfaces, and the monotonic advance leaves the future
        marker untouched (never rewound).
        """
        _merged_ticket()
        future_marker = timezone.now() + timedelta(hours=6)
        advance_checkpoint(future_marker, checkpoint_file)
        out = _call("checking", "show", "--this-overlay")
        assert "Merged" in out  # the recent merge is NOT skipped
        # The marker is not rewound to ``now`` (which would lose the bound).
        assert load_checkpoint(checkpoint_file) == future_marker


class TestCheckingShowAllOverlays:
    """``checking show`` default = all overlays; ``--this-overlay`` = current overlay only."""

    def _merged_ticket_for(self, overlay: str, *, number: int, pr_id: int, slug: str) -> Ticket:
        ticket = Ticket.objects.create(
            overlay=overlay,
            issue_url=f"https://github.com/{slug}/issues/{number}",
            state=Ticket.State.IN_REVIEW,
            short_description=f"{overlay} work",
        )
        clear = MergeClear.issue(
            ClearRequest(
                pr_id=pr_id,
                slug=slug,
                reviewed_sha=_SHA,
                reviewer_identity="cold-reviewer",
                ticket=ticket,
            ),
        )
        MergeAudit.objects.create(clear=clear, merged_sha=_SHA, required_checks_status="success")
        return ticket

    def test_all_overlays_aggregation(self, tmp_path: Path) -> None:
        """Default path reports merged tickets from both overlays."""
        self._merged_ticket_for("acme", number=1, pr_id=10, slug="acme/widgets")
        self._merged_ticket_for("beta", number=2, pr_id=20, slug="beta/core")

        with _two_overlay_patch():
            out = _call("checking", "show", "--no-advance")
        assert "acme/widgets#10" in out
        assert "beta/core#20" in out

    def test_this_overlay_flag_scopes_to_one(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--this-overlay`` shows only the active overlay; the other's checkpoint is untouched."""
        self._merged_ticket_for("acme", number=1, pr_id=10, slug="acme/widgets")
        self._merged_ticket_for("beta", number=2, pr_id=20, slug="beta/core")

        acme_ckpt = tmp_path / "acme_ckpt.json"
        beta_ckpt = tmp_path / "beta_ckpt.json"

        def _fake_checkpoint_path(*, overlay: str | None = None) -> Path:
            import os  # noqa: PLC0415

            name = overlay if overlay is not None else os.environ.get("T3_OVERLAY_NAME", "")
            if name == "acme":
                return acme_ckpt
            if name == "beta":
                return beta_ckpt
            return tmp_path / f"checking_checkpoint_{name or 'global'}.json"

        monkeypatch.setattr("teatree.core.checkpoint.checkpoint_path", _fake_checkpoint_path)
        monkeypatch.setattr("teatree.core.management.commands.checking.checkpoint_path", _fake_checkpoint_path)

        with _two_overlay_patch():
            out = _call("checking", "show", "--this-overlay")

        assert "acme/widgets#10" in out
        assert "beta/core#20" not in out
        assert not beta_ckpt.exists()

    def test_per_overlay_marker_independence(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Advancing one overlay's marker must not affect the other's checkpoint file."""
        acme_ckpt = tmp_path / "acme_ckpt.json"
        beta_ckpt = tmp_path / "beta_ckpt.json"

        def _fake_checkpoint_path(*, overlay: str | None = None) -> Path:
            import os  # noqa: PLC0415

            name = overlay if overlay is not None else os.environ.get("T3_OVERLAY_NAME", "")
            if name == "acme":
                return acme_ckpt
            if name == "beta":
                return beta_ckpt
            return tmp_path / f"checking_checkpoint_{name or 'global'}.json"

        monkeypatch.setattr("teatree.core.checkpoint.checkpoint_path", _fake_checkpoint_path)
        monkeypatch.setattr("teatree.core.management.commands.checking.checkpoint_path", _fake_checkpoint_path)

        with _two_overlay_patch():
            _call("checking", "show")

        assert acme_ckpt.exists()
        assert beta_ckpt.exists()
        acme_ts = load_checkpoint(acme_ckpt)
        beta_ts = load_checkpoint(beta_ckpt)
        assert acme_ts is not None
        assert beta_ts is not None

    def test_all_overlays_json_has_all_overlays_shape(self, tmp_path: Path) -> None:
        """``--json`` on the all-overlays path returns the AllOverlaysReportDict shape."""
        self._merged_ticket_for("acme", number=1, pr_id=10, slug="acme/widgets")

        with _two_overlay_patch():
            out = _call("checking", "show", "--no-advance", "--json")

        payload = json.loads(out)
        assert "all_overlays" in payload
        assert "merged" in payload
