"""`t3 ticket plan-reaffirm` — the stale-plan remediation (SELFCATCH-3, anti-vacuity proof c).

When a plan goes stale (its base moved off HEAD and intervening commits touched a
declared seam), the plan-currency gate names ``plan-reaffirm`` as the never-lockout
escape. Reaffirm appends a NEW PlanArtifact re-bound to the new base but REFUSES
unless a ``--disposition`` is supplied per intervening seam-touching commit — a
stale-base re-bind must reckon with what moved, never rubber-stamp it. After a
disposition, the ticket is current again and ``code()`` proceeds.
"""

import contextlib
import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.gates import plan_currency_gate
from teatree.core.gates.plan_currency_gate import check_plan_current
from teatree.core.management.commands._plan_gate_commands import ReaffirmError, reaffirm_plan
from teatree.core.models import Ticket, Worktree
from teatree.core.models.plan_artifact import PlanArtifact

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


def _advance_remote(tmp_path: Path, bare: Path, *, path: str) -> None:
    work = tmp_path / f"advance-{Path(path).name}"
    _git(tmp_path, "clone", str(bare), str(work))
    _git(work, "config", "user.email", "t@e.st")  # privacy-scan:allow
    _git(work, "config", "user.name", "Tester")
    target = work / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed\n")
    _git(work, "add", path)
    _git(work, "commit", "-m", f"remote: touch {path}")
    _git(work, "push", "origin", "main")


def _adequacy() -> dict:
    return {
        "design": {"content": "implement the change"},
        "integration_seams": {"content": [_SEAM]},
        "edge_cases": {"content": ["stale base"]},
        "test_strategy": {"content": "red-first"},
    }


@contextlib.contextmanager
def _gate(*, required: bool) -> Iterator[None]:
    with patch.object(plan_currency_gate, "plan_adequacy_required", return_value=required):
        yield


class TestPlanReaffirm(TestCase):
    def setUp(self) -> None:
        self._tmp = Path(__import__("tempfile").mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self._tmp, ignore_errors=True))
        self.bare = _make_remote(self._tmp)
        self.clone = _clone(self._tmp, self.bare)
        self.old_base = _git(self.clone, "rev-parse", "HEAD")
        self.ticket = Ticket.objects.create(overlay="acme", role=Ticket.Role.AUTHOR, state=Ticket.State.PLANNED)
        Worktree.objects.create(
            ticket=self.ticket, repo_path=str(self.clone), branch="feature", extra={"worktree_path": str(self.clone)}
        )
        PlanArtifact.objects.create(
            ticket=self.ticket,
            plan_text="real plan",
            recorded_by="t3:planner",
            base_sha=self.old_base,
            adequacy=_adequacy(),
        )
        _advance_remote(self._tmp, self.bare, path=_SEAM)  # stale on a seam
        _git(self.clone, "fetch", "origin")
        self.new_base = _git(self.clone, "rev-parse", "origin/main")

    def test_reaffirm_refused_without_a_disposition(self) -> None:
        with pytest.raises(ReaffirmError, match="disposition"):
            reaffirm_plan(ticket=self.ticket, new_base_sha=self.new_base, dispositions=[], by="op")

    def test_reaffirm_refused_with_non_hex_base(self) -> None:
        with pytest.raises(ReaffirmError, match="40-char hex"):
            reaffirm_plan(ticket=self.ticket, new_base_sha="not-a-sha", dispositions=["x"], by="op")

    def test_reaffirm_succeeds_after_disposition_and_rebinds_the_plan(self) -> None:
        """PROOF c: a disposition per intervening seam commit re-binds the plan to the new base."""
        artifact = reaffirm_plan(
            ticket=self.ticket,
            new_base_sha=self.new_base,
            dispositions=["seam change reviewed; plan still holds"],
            by="op",
        )
        assert artifact.base_sha == self.new_base
        assert artifact.adequacy["integration_seams"]["content"] == [_SEAM]  # carried forward
        # the ticket is current again → the currency gate now passes.
        with _gate(required=True):
            assert check_plan_current(self.ticket) is True

    def test_reaffirm_refused_when_no_plan_exists(self) -> None:
        fresh = Ticket.objects.create(overlay="acme", role=Ticket.Role.AUTHOR, state=Ticket.State.PLANNED)
        with pytest.raises(ReaffirmError, match="no plan to reaffirm"):
            reaffirm_plan(ticket=fresh, new_base_sha="a" * 40, dispositions=["x"], by="op")
