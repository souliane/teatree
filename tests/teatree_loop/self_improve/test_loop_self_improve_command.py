"""End-to-end integration test for the ``loop_self_improve`` mgmt command.

Drives the full path: seed a smell, ``call_command('loop_self_improve',
tier='cheap')``, then assert a ``SelfImproveFiring`` row is recorded for
at least one detector.

The forgotten-merge smell is a *stalled* merge, and since #4250 that means the forge
reports the PR OPEN — a missing ``MergeAudit`` is not evidence on its own. The reader
the cheap tier arms is therefore replaced with an OPEN-reading fake: no test may reach
a forge (the CI test image has no ``gh``), and leaving it unstubbed would make the
assertion below unsatisfiable rather than merely unverified.
"""

import datetime as dt
import io
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from teatree.core.gates.t3_master_gate import T3MasterGate
from teatree.core.loop_lease_manager import T3_MASTER_SLOT
from teatree.core.models import LoopLease, MergeClear, SelfImproveFiring, Ticket
from teatree.core.models.merge_clear import ClearRequest
from teatree.core.models.pull_request import PullRequest
from teatree.loop.self_improve import schedule as schedule_mod
from teatree.loop.self_improve.actions import ActionResult
from teatree.loop.self_improve.budget import BudgetVerdict
from teatree.loop.self_improve.schedule import TierResult
from tests._loop_principal_env import pinned_loop_principal
from tests._t3_master_env import worker_owns_t3_master

_ARMED_READER = "teatree.loop.self_improve.schedule.live_pr_state_reader"


def _reads_open() -> object:
    def read(pr_url: str) -> str:
        return "open"

    return read


class LoopSelfImproveCommandTests(TestCase):
    """End-to-end coverage for the ``loop_self_improve`` mgmt command.

    Pin the RAM probe to a deterministic low value so the budget gate
    never skips the cycle just because the host happens to be loaded —
    same seam used by :mod:`tests.teatree_loop.self_improve.test_budget`.
    """

    def setUp(self) -> None:
        super().setUp()
        self._ram_patch = patch(
            "teatree.loop.self_improve.budget._read_ram_used_percent",
            return_value=10.0,
        )
        self._ram_patch.start()
        self.addCleanup(self._ram_patch.stop)
        self._disk_patch = patch("teatree.loop.self_improve.budget.read_disk_used_percent", return_value=10.0)
        self._disk_patch.start()
        self.addCleanup(self._disk_patch.stop)
        # Stand in for the worker: the cycle runs only under a live t3-master owner (#3968).
        self.enterContext(worker_owns_t3_master())

    def test_command_writes_firing_row_for_seeded_smell(self) -> None:
        # Seed a forgotten-merge smell: CLEAR > 30 min old, no audit.
        clear = MergeClear.issue(
            ClearRequest(
                pr_id=999,
                slug="souliane/teatree",
                reviewed_sha="deadbeefcafe1234" + "0" * 24,
                reviewer_identity="reviewer@example.com",
                gh_verify_result="green",
                blast_class="logic",
            )
        )
        old = timezone.now() - dt.timedelta(hours=1)
        MergeClear.objects.filter(pk=clear.pk).update(issued_at=old)

        out = io.StringIO()
        with patch(_ARMED_READER, _reads_open):
            call_command("loop_self_improve", tier="cheap", stdout=out, stderr=out)

        # The forgotten_merge detector must have written a firing.
        assert SelfImproveFiring.objects.filter(detector="forgotten_merge").count() == 1
        # The cycle ran; missing unrelated local evidence is reported as degraded.
        assert any(status in out.getvalue() for status in ("OK", "SKIP", "DEGRADED"))

    def test_red_budget_with_safe_action_is_not_reported_as_a_total_skip(self) -> None:
        result = TierResult(
            tier="cheap",
            budget=BudgetVerdict.skip("low_ram"),
            actions=[
                ActionResult(
                    rung="ticket",
                    firing=SimpleNamespace(pk=1, detector="pressure_incident", dedup_key="pressure_incident::memory"),
                )
            ],
        )
        out = io.StringIO()
        with patch("teatree.loop.self_improve.schedule.run_tier", return_value=result):
            call_command("loop_self_improve", tier="cheap", stdout=out, stderr=out)

        assert "DEGRADED" in out.getvalue()
        assert "actions=1" in out.getvalue()

    def test_command_json_output_includes_reports(self) -> None:
        clear = MergeClear.issue(
            ClearRequest(
                pr_id=1000,
                slug="souliane/teatree",
                reviewed_sha="deadbeefcafe5678" + "0" * 24,
                reviewer_identity="reviewer@example.com",
                gh_verify_result="green",
                blast_class="logic",
            )
        )
        old = timezone.now() - dt.timedelta(hours=1)
        MergeClear.objects.filter(pk=clear.pk).update(issued_at=old)

        out = io.StringIO()
        with patch(_ARMED_READER, _reads_open):
            call_command("loop_self_improve", tier="cheap", json_output=True, stdout=out)

        payload = json.loads(out.getvalue())
        assert payload["tier"] == "cheap"
        # Either the cycle ran (report_count ≥ 1) or it skipped — both
        # outcomes have the contract keys.
        assert "report_count" in payload
        assert "action_count" in payload

    def test_unknown_scan_is_visible_in_json_and_human_output(self) -> None:
        result = TierResult(
            tier="cheap",
            budget=BudgetVerdict.allow(),
            degraded_scans=[("lifecycle_incident", "otel_missing")],
        )
        json_out = io.StringIO()
        human_err = io.StringIO()
        with patch.object(schedule_mod, "run_tier", return_value=result):
            call_command("loop_self_improve", tier="cheap", json_output=True, stdout=json_out)
            call_command("loop_self_improve", tier="cheap", stdout=io.StringIO(), stderr=human_err)

        assert json.loads(json_out.getvalue())["degraded_scans"] == [
            {"detector": "lifecycle_incident", "reason": "otel_missing"}
        ]
        assert "DEGRADED" in human_err.getvalue()
        assert "lifecycle_incident:otel_missing" in human_err.getvalue()

    def test_unbuilt_tier_exits_nonzero_instead_of_reporting_a_clean_cycle(self) -> None:
        for tier in ("medium", "expensive", "phase-99-future"):
            out, err = io.StringIO(), io.StringIO()
            with pytest.raises(SystemExit) as exc:
                call_command("loop_self_improve", tier=tier, stdout=out, stderr=err)

            assert exc.value.code == 2
            assert tier in err.getvalue()
            assert "OK" not in out.getvalue()
            assert SelfImproveFiring.objects.count() == 0
            # Refused before the lease is taken — a no-op cycle never holds it.
            assert not LoopLease.objects.filter(name="loop-self-improve").exists()

    def test_dedicated_slot_invokes_real_rerender_seam_on_stale_statusline(self) -> None:
        """Anti-vacuous: the dedicated slot genuinely self-heals a stale statusline.

        Drives the full ``call_command('loop_self_improve')`` path with a merged-PR
        URL seeded onto the rendered statusline (the ``StaleStatuslineEntryDetector``
        smell) and asserts the real ``self_improve_rerender`` seam is invoked. Before
        #2625 Part B the dedicated slot reached at most the ``statusline`` rung and,
        even there, routed the detector's no-op sentinel — so the seam never ran.
        """
        url = "https://github.com/o/r/pull/7777"
        ticket = Ticket.objects.create(overlay="acme", issue_url=url + "/issues")
        pr = PullRequest.objects.create(ticket=ticket, overlay="acme", url=url, repo="o/r", iid="7777")
        pr.mark_merged()
        pr.save()

        out = io.StringIO()
        with tempfile.TemporaryDirectory() as data_home:
            teatree_dir = Path(data_home) / "teatree"
            teatree_dir.mkdir()
            (teatree_dir / "statusline.txt").write_text(f"in flight: {url}", encoding="utf-8")
            with (
                patch.dict(os.environ, {"XDG_DATA_HOME": data_home}),
                patch("teatree.loop.phases.render.self_improve_rerender") as seam,
            ):
                call_command("loop_self_improve", tier="cheap", stdout=out, stderr=out)

        seam.assert_called_once()
        assert SelfImproveFiring.objects.filter(detector="stale_statusline_entry").count() == 1


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestSelfImproveT3MasterGate:
    """The gate names WHICH condition stopped the cycle (#3968).

    "nothing owns this" and "another live session owns this" call for opposite
    operator responses; the pre-#3968 wording ("this session is not the loop owner")
    reported both as the second and sent the operator hunting a session that did not
    exist.
    """

    def test_unclaimed_slot_skips_and_says_so(self) -> None:
        out = io.StringIO()
        err = io.StringIO()
        call_command("loop_self_improve", tier="cheap", json_output=True, stdout=out)
        call_command("loop_self_improve", tier="cheap", stdout=io.StringIO(), stderr=err)

        payload = json.loads(out.getvalue())
        assert payload["skipped"] is True
        assert payload["skipped_reason"] == T3MasterGate.UNCLAIMED.value
        assert payload["owner_session"] == ""
        assert "`t3-master` owner lease is unheld" in err.getvalue()

    def test_foreign_live_session_skips_and_names_the_owner(self) -> None:
        LoopLease.objects.claim_ownership(T3_MASTER_SLOT, session_id="sess-other", owner_pid=os.getpid())
        out = io.StringIO()
        err = io.StringIO()
        with pinned_loop_principal("sess-mine"):
            call_command("loop_self_improve", tier="cheap", json_output=True, stdout=out)
            call_command("loop_self_improve", tier="cheap", stdout=io.StringIO(), stderr=err)

        payload = json.loads(out.getvalue())
        assert payload["skipped_reason"] == T3MasterGate.FOREIGN_OWNER.value
        assert payload["owner_session"] == "sess-other"
        assert "another live session" in err.getvalue()
        assert "sess-other" in err.getvalue()

    def test_worker_owned_slot_lets_the_cycle_run(self) -> None:
        out = io.StringIO()
        with worker_owns_t3_master(), pinned_loop_principal("sess-unrelated"):
            call_command("loop_self_improve", tier="cheap", json_output=True, stdout=out)

        payload = json.loads(out.getvalue())
        assert payload.get("skipped_reason") != T3MasterGate.UNCLAIMED.value
        assert "report_count" in payload
