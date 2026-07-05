"""plan_currency FSM gate: coding unreachable without an adequate, current-HEAD-bound plan.

SELFCATCH-3 — forecloses the named root cause of the 26-bug integration campaign:
thin-spec-as-plan and stale-base coding. Two anti-vacuity proofs. First, a plan
bound to a STALE base whose intervening commits touch a DECLARED seam is treated
ABSENT, so code() / schedule_coding refuse (test_code_refused_when_stale,
test_schedule_coding_refused_when_stale) — proven load-bearing by
test_gate_is_load_bearing (neutralise the gate → the same stale plan advances).
Second, an inadequate/legacy plan is treated absent → refuse.

Real ``git init`` under ``tmp_path`` (mirrors ``test_branch_currency``) drives the
stale-on-a-seam path deterministically; the flag ships OFF so the generic FSM is
never blocked.
"""

import contextlib
import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.gates import plan_currency_gate
from teatree.core.gates.plan_currency_gate import check_plan_current, is_bound_to
from teatree.core.modelkit import gate_registry
from teatree.core.models import Ticket, Worktree
from teatree.core.models.errors import NoCurrentPlanError
from teatree.core.models.plan_artifact import PlanArtifact
from teatree.core.models.trivial_plan_skip import mark_trivial_plan_skip

_SEAM = "src/seam.py"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],  # noqa: S607 — `git` from PATH is intended; test-only helper over tmp_path repos.
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _make_remote(tmp_path: Path) -> Path:
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.email", "t@e.st")  # privacy-scan:allow
    _git(seed, "config", "user.name", "Tester")
    (seed / "a.txt").write_text("base\n")
    _git(seed, "add", "a.txt")
    _git(seed, "commit", "-m", "initial")
    bare = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(seed), str(bare))
    return bare


def _clone(tmp_path: Path, bare: Path) -> Path:
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(bare), str(clone))
    _git(clone, "config", "user.email", "t@e.st")  # privacy-scan:allow
    _git(clone, "config", "user.name", "Tester")
    return clone


def _advance_remote(tmp_path: Path, bare: Path, *, path: str, content: str = "changed\n") -> None:
    work = tmp_path / f"advance-{Path(path).name}"
    _git(tmp_path, "clone", str(bare), str(work))
    _git(work, "config", "user.email", "t@e.st")  # privacy-scan:allow
    _git(work, "config", "user.name", "Tester")
    target = work / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _git(work, "add", path)
    _git(work, "commit", "-m", f"remote: touch {path}")
    _git(work, "push", "origin", "main")


def _adequacy(seams: list[str]) -> dict:
    return {
        "design": {"content": "implement the change"},
        "integration_seams": {"content": seams} if seams else {"none_reason": "pure refactor"},
        "edge_cases": {"content": ["offline fetch"]},
        "test_strategy": {"content": "red-first"},
    }


@contextlib.contextmanager
def _gate(*, required: bool) -> Iterator[None]:
    with patch.object(plan_currency_gate, "plan_adequacy_required", return_value=required):
        yield


def _planned_ticket_with_worktree(clone: Path, *, base_sha: str, seams: list[str]) -> Ticket:
    ticket = Ticket.objects.create(overlay="acme", role=Ticket.Role.AUTHOR, state=Ticket.State.PLANNED)
    Worktree.objects.create(
        ticket=ticket,
        repo_path=str(clone),
        branch="feature",
        extra={"worktree_path": str(clone)},
    )
    PlanArtifact.objects.create(
        ticket=ticket,
        plan_text="real plan",
        recorded_by="t3:planner",
        base_sha=base_sha,
        adequacy=_adequacy(seams),
    )
    return ticket


class TestIsBoundTo(TestCase):
    def test_exact_match_is_bound(self) -> None:
        artifact = PlanArtifact(base_sha="a" * 40)
        assert is_bound_to(artifact, "a" * 40) is True

    def test_case_insensitive(self) -> None:
        artifact = PlanArtifact(base_sha="A" * 40)
        assert is_bound_to(artifact, "a" * 40) is True

    def test_blank_never_matches(self) -> None:
        assert is_bound_to(PlanArtifact(base_sha=""), "a" * 40) is False
        assert is_bound_to(PlanArtifact(base_sha="a" * 40), "") is False


class TestCheckPlanCurrent(TestCase):
    def test_flag_off_is_a_noop_even_with_a_stale_plan(self) -> None:
        # A stale plan passes when the gate is off — opt-in, generic FSM green.
        ticket = Ticket.objects.create(overlay="acme", role=Ticket.Role.AUTHOR, state=Ticket.State.PLANNED)
        PlanArtifact.objects.create(ticket=ticket, plan_text="p", recorded_by="op")  # legacy blank-sha
        with _gate(required=False):
            assert check_plan_current(ticket) is True

    def test_inadequate_legacy_plan_is_refused_when_on(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", role=Ticket.Role.AUTHOR, state=Ticket.State.PLANNED)
        PlanArtifact.objects.create(ticket=ticket, plan_text="p", recorded_by="op")  # blank sha, empty adequacy
        with _gate(required=True), pytest.raises(NoCurrentPlanError, match="not adequate"):
            check_plan_current(ticket)

    def test_no_plan_at_all_passes_absence_is_the_plan_first_gates_job(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", role=Ticket.Role.AUTHOR, state=Ticket.State.PLANNED)
        with _gate(required=True):
            assert check_plan_current(ticket) is True

    def test_trivial_skip_marker_passes(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", role=Ticket.Role.AUTHOR, state=Ticket.State.PLANNED)
        mark_trivial_plan_skip(ticket, reason="typo fix", by="op")
        ticket.save()
        with _gate(required=True):
            assert check_plan_current(ticket) is True

    def test_no_worktree_fails_open(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", role=Ticket.Role.AUTHOR, state=Ticket.State.PLANNED)
        PlanArtifact.objects.create(
            ticket=ticket, plan_text="p", recorded_by="op", base_sha="a" * 40, adequacy=_adequacy([_SEAM])
        )
        with _gate(required=True):
            assert check_plan_current(ticket) is True  # no materialised worktree → undeterminable → open


class TestCheckPlanCurrentGit(TestCase):
    """The deterministic git-backed currency proofs (real repos under tmp_path)."""

    def setUp(self) -> None:
        self._tmp = Path(__import__("tempfile").mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self._tmp, ignore_errors=True))
        self.bare = _make_remote(self._tmp)
        self.clone = _clone(self._tmp, self.bare)
        self.base_sha = _git(self.clone, "rev-parse", "HEAD")

    def test_current_plan_bound_to_head_passes(self) -> None:
        ticket = _planned_ticket_with_worktree(self.clone, base_sha=self.base_sha, seams=[_SEAM])
        with _gate(required=True):
            assert check_plan_current(ticket) is True  # base == live HEAD → bound

    def test_stale_base_touching_a_declared_seam_is_refused(self) -> None:
        """ANTI-VACUITY PROOF b: HEAD moved off base AND touched a declared seam → ABSENT."""
        ticket = _planned_ticket_with_worktree(self.clone, base_sha=self.base_sha, seams=[_SEAM])
        _advance_remote(self._tmp, self.bare, path=_SEAM)
        with _gate(required=True), pytest.raises(NoCurrentPlanError, match="STALE"):
            check_plan_current(ticket)

    def test_base_moved_but_not_on_a_seam_passes(self) -> None:
        ticket = _planned_ticket_with_worktree(self.clone, base_sha=self.base_sha, seams=[_SEAM])
        _advance_remote(self._tmp, self.bare, path="src/unrelated.py")
        with _gate(required=True):
            assert check_plan_current(ticket) is True  # moved, but no declared seam touched


class TestFsmIntegration(TestCase):
    """code() / schedule_coding refuse a stale plan; the gate is proven load-bearing."""

    def setUp(self) -> None:
        self._tmp = Path(__import__("tempfile").mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self._tmp, ignore_errors=True))
        self.bare = _make_remote(self._tmp)
        self.clone = _clone(self._tmp, self.bare)
        self.base_sha = _git(self.clone, "rev-parse", "HEAD")
        self.ticket = _planned_ticket_with_worktree(self.clone, base_sha=self.base_sha, seams=[_SEAM])
        _advance_remote(self._tmp, self.bare, path=_SEAM)  # make the plan stale on a seam

    def test_code_refused_when_stale(self) -> None:
        with _gate(required=True), pytest.raises(NoCurrentPlanError):
            self.ticket.code()
        self.ticket.refresh_from_db()
        assert self.ticket.state == Ticket.State.PLANNED  # did NOT advance

    def test_schedule_coding_refused_when_stale(self) -> None:
        with _gate(required=True), pytest.raises(NoCurrentPlanError):
            self.ticket.schedule_coding()

    def test_gate_is_load_bearing(self) -> None:
        """Neutralise the gate → the SAME stale plan advances PLANNED → CODED."""
        neutralised = {**gate_registry._REGISTRY, ("gate", "plan_currency"): lambda _t: True}
        with (
            _gate(required=True),
            patch.object(gate_registry, "_REGISTRY", neutralised),
            self.captureOnCommitCallbacks(execute=False),
        ):
            self.ticket.code()
            self.ticket.save()
        self.ticket.refresh_from_db()
        assert self.ticket.state == Ticket.State.CODED


class TestRegistration(TestCase):
    def test_plan_currency_gate_is_registered(self) -> None:
        assert gate_registry.get_gate("plan_currency") is check_plan_current
