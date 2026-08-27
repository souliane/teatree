"""Assemble the ``scenario-plan`` file from overlay seams — ``--from-seams`` (#3329)."""

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase
from PIL import Image

from teatree.core.e2e_scenario import Capture, Scenario
from teatree.core.intake.e2e_workitem import RunProvenance, record_run
from teatree.core.management.commands._test_plan import from_seams as _from_seams
from teatree.core.management.commands._test_plan.write import PlanWriteResult
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay import OverlayE2E, OverlayMetadata
from tests.teatree_core.conftest import CommandOverlay

_ISSUE_URL = "https://gitlab.com/org/repo/-/issues/8521"
_TICKET_NUMBER = "8521"
_SPEC = "e2e/specs/login.spec.ts"
_E2E_REPO = "client-workspace"


class _SeamsE2E(OverlayE2E):
    def scenarios(self, spec_path: str) -> tuple[Scenario, ...]:
        if spec_path != _SPEC:
            return ()
        return (
            Scenario(
                surface="Login",
                title="Login works",
                preconditions="signed out",
                steps=("open the page", "submit"),
                expected="dashboard renders",
                captures=(Capture(slot="step1", caption="the login form"),),
            ),
        )


class _SeamsMetadata(OverlayMetadata):
    def get_e2e_config(self) -> dict[str, str]:
        return {"runner": "external", "project_path": f"org/{_E2E_REPO}", "e2e_dir": "e2e"}


class _SeamsOverlay(CommandOverlay):
    e2e = _SeamsE2E()
    metadata = _SeamsMetadata()

    def get_repos(self) -> list[str]:
        return [_E2E_REPO]


_MOCK_OVERLAY = {"test": _SeamsOverlay()}


class _FromSeamsBase(TestCase):
    @pytest.fixture(autouse=True)
    def _inject(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._monkeypatch = monkeypatch
        self._tmp = tmp_path
        # IMMEDIATE on-behalf mode: the post is not gated for the test's lifetime.
        monkeypatch.setenv("T3_ON_BEHALF_POST_MODE", "immediate")
        # No worktree — the ticket resolves via the explicit --ticket ref.
        monkeypatch.setattr(
            _from_seams,
            "resolve_worktree",
            MagicMock(side_effect=_from_seams.WorktreeNotFoundError("none")),
        )

    @property
    def _plan_path(self) -> Path:
        return self._tmp / "checkout" / "test-plans" / f"repo-{_TICKET_NUMBER}.md"

    def _ticket_with_recipe(self, *, spec: str = _SPEC, shas: dict[str, str] | None = None) -> Path:
        ticket = Ticket.objects.create(overlay="test", issue_url=_ISSUE_URL)
        checkout = self._tmp / "checkout"
        checkout.mkdir(exist_ok=True)
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path=_E2E_REPO,
            branch="8521-feat-thing",
            extra={"worktree_path": str(checkout)},
        )
        artifacts_root = self._tmp / "artifacts"
        record_run(
            ticket,
            result="green",
            per_repo_shas=shas if shas is not None else {"backend": "abc1234"},
            env="local",
            provenance=RunProvenance(spec_path=spec, artifacts_dir=str(artifacts_root)),
        )
        return artifacts_root

    def _write_capture(self, artifacts_root: Path, *, slot: str = "step1") -> None:
        env_dir = artifacts_root / _TICKET_NUMBER / "local"
        env_dir.mkdir(parents=True, exist_ok=True)
        (env_dir / f"{slot}.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    def _run(self, *, ticket: str, spec_path: str, artifacts_dir: str) -> PlanWriteResult:
        request = _from_seams.FromSeamsRequest(ticket=ticket, spec_path=spec_path, artifacts_dir=artifacts_dir)
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            return _from_seams.run_from_seams(request, write_err=lambda _s: None)


class TestAssembleAndWrite(_FromSeamsBase):
    def test_creates_scenario_plan_file_from_seams(self) -> None:
        artifacts_root = self._ticket_with_recipe()
        self._write_capture(artifacts_root)

        result = self._run(ticket=_ISSUE_URL, spec_path="", artifacts_dir="")

        assert result["action"] == "created"
        assert result["envs"] == ["local"]
        assert result["path"] == str(self._plan_path)
        body = self._plan_path.read_text(encoding="utf-8")
        assert "<!-- t3-e2e-evidence ticket=8521 -->" in body
        assert "### Scenario 1 — Login" in body
        # The declared capture is cited by its artifacts-root-relative path, with its caption.
        assert "`8521/local/step1.png`" in body
        assert "the login form" in body
        # The recorded SHA renders in the environment footer.
        assert "backend `abc1234`" in body

    def test_rerun_updates_the_single_file_in_place(self) -> None:
        artifacts_root = self._ticket_with_recipe()
        self._write_capture(artifacts_root)
        self._run(ticket=_ISSUE_URL, spec_path="", artifacts_dir="")

        result = self._run(ticket=_ISSUE_URL, spec_path="", artifacts_dir="")

        assert result["action"] == "updated"
        assert [p.name for p in self._plan_path.parent.iterdir()] == [f"repo-{_TICKET_NUMBER}.md"]


class TestFailLoud(_FromSeamsBase):
    def test_no_authored_scenarios_exits_nonzero_nothing_written(self) -> None:
        self._ticket_with_recipe(spec="e2e/specs/unknown.spec.ts")
        with pytest.raises(SystemExit):
            self._run(ticket=_ISSUE_URL, spec_path="", artifacts_dir="")
        assert not self._plan_path.exists()

    def test_missing_capture_file_exits_nonzero_nothing_written(self) -> None:
        self._ticket_with_recipe()  # no capture written
        with pytest.raises(SystemExit):
            self._run(ticket=_ISSUE_URL, spec_path="", artifacts_dir="")
        assert not self._plan_path.exists()

    def test_no_per_repo_shas_exits_nonzero(self) -> None:
        self._ticket_with_recipe(shas={})
        with pytest.raises(SystemExit):
            self._run(ticket=_ISSUE_URL, spec_path="", artifacts_dir="")
        assert not self._plan_path.exists()


class TestResolveSeamsRun(_FromSeamsBase):
    def test_defaults_spec_and_artifacts_to_the_recipe(self) -> None:
        artifacts_root = self._ticket_with_recipe()
        ticket = Ticket.objects.get(issue_url=_ISSUE_URL)
        run = _from_seams.resolve_seams_run(ticket, spec_path="", artifacts_dir="")
        assert run.spec_path == _SPEC
        assert run.artifacts_root == artifacts_root
        assert run.env == "local"
        assert run.per_repo_shas == {"backend": "abc1234"}


class TestWriteTestPlanFromSeamsCommand(_FromSeamsBase):
    def test_command_from_seams_flag_assembles_and_writes(self) -> None:
        artifacts_root = self._ticket_with_recipe()
        self._write_capture(artifacts_root)
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = cast(
                "dict[str, object]",
                call_command("e2e", "write-plan-from-seams", ticket=_ISSUE_URL),
            )
        assert result["action"] == "created"
        assert self._plan_path.is_file()


class TestCommittedCapturesAreGated(_FromSeamsBase):
    """The evidence beside this plan is re-validated here too — the claim holds on every write path."""

    def _commit_capture_with_no_red_box(self) -> None:
        evidence_dir = self._plan_path.parent / "evidence" / self._plan_path.stem
        evidence_dir.mkdir(parents=True)
        Image.new("RGB", (400, 300), (240, 240, 240)).save(evidence_dir / "hand-dropped.png", "PNG")

    def test_a_hand_placed_capture_with_no_red_box_refuses_the_write(self) -> None:
        artifacts_root = self._ticket_with_recipe()
        self._write_capture(artifacts_root)
        self._commit_capture_with_no_red_box()

        with pytest.raises(SystemExit):
            self._run(ticket=_ISSUE_URL, spec_path="", artifacts_dir="")

        assert not self._plan_path.exists()
