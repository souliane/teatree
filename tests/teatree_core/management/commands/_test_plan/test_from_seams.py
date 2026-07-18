"""Assemble the ``scenario-plan`` note from overlay seams — ``--from-seams`` (#3329)."""

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.backend_protocols import UploadVerification
from teatree.core.e2e_scenario import Capture, Scenario
from teatree.core.intake.e2e_workitem import RunProvenance, record_run
from teatree.core.management.commands._test_plan import from_seams as _from_seams
from teatree.core.management.commands._test_plan.post import PostTestPlanResult
from teatree.core.models import Ticket
from teatree.core.overlay import OverlayE2E
from tests.teatree_core.conftest import CommandOverlay

_ISSUE_URL = "https://gitlab.com/org/repo/-/issues/8521"
_TICKET_NUMBER = "8521"
_SPEC = "e2e/specs/login.spec.ts"


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


class _SeamsOverlay(CommandOverlay):
    e2e = _SeamsE2E()


_MOCK_OVERLAY = {"test": _SeamsOverlay()}


def _fake_host() -> MagicMock:
    host = MagicMock()
    host.repo_for_issue_url.return_value = "org/repo"
    host.list_issue_comments.return_value = []
    host.post_issue_comment.return_value = {"id": 99, "web_url": "u"}
    host.upload_file.return_value = {"full_path": "/-/project/9/uploads/x/step1.png"}
    host.verify_upload.return_value = UploadVerification(ok=True, embed_url="/uploads/x/step1.png")
    return host


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

    def _ticket_with_recipe(self, *, spec: str = _SPEC, shas: dict[str, str] | None = None) -> Path:
        ticket = Ticket.objects.create(overlay="test", issue_url=_ISSUE_URL)
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

    def _run(self, host: MagicMock, *, ticket: str, spec_path: str, artifacts_dir: str) -> PostTestPlanResult:
        self._monkeypatch.setattr(_from_seams, "code_host_from_overlay", lambda: host)
        request = _from_seams.FromSeamsRequest(ticket=ticket, spec_path=spec_path, artifacts_dir=artifacts_dir)
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            return _from_seams.run_from_seams(request, write_out=lambda _s: None, write_err=lambda _s: None)


class TestAssembleAndPost(_FromSeamsBase):
    def test_creates_scenario_plan_note_from_seams(self) -> None:
        artifacts_root = self._ticket_with_recipe()
        self._write_capture(artifacts_root)
        host = _fake_host()

        result = self._run(host, ticket=_ISSUE_URL, spec_path="", artifacts_dir="")

        assert result["action"] == "created"
        assert result["envs"] == ["local"]
        host.post_issue_comment.assert_called_once()
        body = host.post_issue_comment.call_args.kwargs["body"]
        assert "<!-- t3-e2e-evidence ticket=8521 -->" in body
        assert "### Scenario 1 — Login" in body
        # The declared capture's uploaded embed and its caption render.
        assert "](/uploads/x/step1.png)" in body
        assert "the login form" in body
        # The recorded SHA renders in the environment footer.
        assert "backend `abc1234`" in body

    def test_rerun_updates_the_single_note_in_place(self) -> None:
        artifacts_root = self._ticket_with_recipe()
        self._write_capture(artifacts_root)
        host = _fake_host()
        host.list_issue_comments.return_value = [{"id": 55, "body": "<!-- t3-e2e-evidence ticket=8521 -->\nprior"}]
        host.update_issue_comment.return_value = {"id": 55}

        result = self._run(host, ticket=_ISSUE_URL, spec_path="", artifacts_dir="")

        assert result["action"] == "updated"
        assert result["comment_id"] == 55
        host.update_issue_comment.assert_called_once()
        host.post_issue_comment.assert_not_called()


class TestFailLoud(_FromSeamsBase):
    def test_no_authored_scenarios_exits_nonzero_no_post(self) -> None:
        self._ticket_with_recipe(spec="e2e/specs/unknown.spec.ts")
        host = _fake_host()
        with pytest.raises(SystemExit):
            self._run(host, ticket=_ISSUE_URL, spec_path="", artifacts_dir="")
        host.post_issue_comment.assert_not_called()

    def test_missing_capture_file_exits_nonzero_no_post(self) -> None:
        self._ticket_with_recipe()  # no capture written
        host = _fake_host()
        with pytest.raises(SystemExit):
            self._run(host, ticket=_ISSUE_URL, spec_path="", artifacts_dir="")
        host.post_issue_comment.assert_not_called()

    def test_no_per_repo_shas_exits_nonzero(self) -> None:
        self._ticket_with_recipe(shas={})
        host = _fake_host()
        with pytest.raises(SystemExit):
            self._run(host, ticket=_ISSUE_URL, spec_path="", artifacts_dir="")
        host.post_issue_comment.assert_not_called()


class TestResolveSeamsRun(_FromSeamsBase):
    def test_defaults_spec_and_artifacts_to_the_recipe(self) -> None:
        artifacts_root = self._ticket_with_recipe()
        ticket = Ticket.objects.get(issue_url=_ISSUE_URL)
        run = _from_seams.resolve_seams_run(ticket, spec_path="", artifacts_dir="")
        assert run.spec_path == _SPEC
        assert run.artifacts_root == artifacts_root
        assert run.env == "local"
        assert run.per_repo_shas == {"backend": "abc1234"}


class TestPostTestPlanFromSeamsCommand(_FromSeamsBase):
    def test_command_from_seams_flag_assembles_and_posts(self) -> None:
        artifacts_root = self._ticket_with_recipe()
        self._write_capture(artifacts_root)
        host = _fake_host()
        self._monkeypatch.setattr(_from_seams, "code_host_from_overlay", lambda: host)
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = cast(
                "dict[str, object]",
                call_command("e2e", "post-test-plan", from_seams=True, ticket=_ISSUE_URL),
            )
        assert result["action"] == "created"
        host.post_issue_comment.assert_called_once()
