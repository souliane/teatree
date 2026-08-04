"""``ShipExecutor`` honours the ``target_branch`` setting on BOTH of its seams.

The setting (#940) makes a whole line of work stack onto ONE long-lived
integration branch. That needs producer↔consumer parity: the branch-currency
re-check must predict against the configured target, and the ``PullRequestSpec``
must carry it — otherwise the gate merges the integration branch into the
worktree and the PR is then opened against the repo default, with every commit
of the integration branch in the diff.
"""

import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.core.models import ConfigSetting, Ticket, Worktree
from teatree.core.runners import ShipExecutor
from tests.teatree_core.conftest import CommandOverlay

_MOCK_OVERLAY = {"test": CommandOverlay()}
_INTEGRATION = "chore/long-lived-integration"
_GIT = shutil.which("git") or "git"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        [_GIT, "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _seed_remote_with_integration_branch(tmp_path: Path) -> Path:
    """Bare remote carrying ``main`` and an ``_INTEGRATION`` branch that edits ``a.txt``."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.email", "t@e.st")  # privacy-scan:allow
    _git(seed, "config", "user.name", "Tester")
    (seed / "a.txt").write_text("base\n")
    _git(seed, "add", "a.txt")
    _git(seed, "commit", "-m", "initial")
    _git(seed, "checkout", "-b", _INTEGRATION)
    (seed / "a.txt").write_text("integration-change\n")
    _git(seed, "add", "a.txt")
    _git(seed, "commit", "-m", "integration: change a.txt")
    _git(seed, "checkout", "main")
    bare = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(seed), str(bare))
    return bare


def _clone_with_conflicting_feature(tmp_path: Path) -> Path:
    """Clone whose ``feat-x`` conflicts with ``origin/<integration>`` but not ``origin/main``."""
    bare = _seed_remote_with_integration_branch(tmp_path)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(bare), str(clone))
    _git(clone, "config", "user.email", "t@e.st")  # privacy-scan:allow
    _git(clone, "config", "user.name", "Tester")
    _git(clone, "checkout", "-b", "feat-x")
    (clone / "a.txt").write_text("feature-change\n")
    _git(clone, "add", "a.txt")
    _git(clone, "commit", "-m", "feat: change a.txt")
    _git(clone, "fetch", "origin")
    return clone


class TestShipTargetsTheConfiguredBranch(TestCase):
    """The configured integration branch reaches both the currency gate and the spec."""

    @pytest.fixture(autouse=True)
    def _inject_tmp(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path

    def _ticket_on(self, clone: Path) -> Ticket:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/940")
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path=str(clone),
            branch="feat-x",
            extra={"worktree_path": str(clone)},
        )
        return ticket

    def _ship(self, ticket: Ticket) -> tuple[object, MagicMock]:
        host = MagicMock()
        host.create_pr.return_value = {"web_url": "https://github.com/souliane/teatree/pull/1"}
        host.current_user.return_value = "souliane"
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.branch_merged", return_value=False),
            patch("teatree.core.runners.ship.push_branch"),
            patch("teatree.core.runners.ship.git.last_commit_message", return_value=("feat: x", "body")),
            patch("teatree.core.runners.ship.git.remote_slug", return_value="souliane/teatree"),
            patch("teatree.core.runners.ship.git.config_value", return_value="souliane"),
        ):
            return ShipExecutor(ticket).run(), host

    def _plain_repo(self) -> Path:
        _git(self.tmp_path, "init", "-b", "main", "plain")
        return self.tmp_path / "plain"

    def test_the_pr_spec_carries_the_configured_target(self) -> None:
        ConfigSetting.objects.set_value("target_branch", _INTEGRATION)
        clone = self._plain_repo()
        ticket = self._ticket_on(clone)

        with patch.object(ShipExecutor, "_check_branch_currency", return_value=None):
            _result, host = self._ship(ticket)

        (spec,) = host.create_pr.call_args.args
        assert spec.target_branch == _INTEGRATION

    def test_an_unset_setting_leaves_the_forge_default_in_charge(self) -> None:
        clone = self._plain_repo()
        ticket = self._ticket_on(clone)

        with patch.object(ShipExecutor, "_check_branch_currency", return_value=None):
            _result, host = self._ship(ticket)

        (spec,) = host.create_pr.call_args.args
        assert spec.target_branch == ""

    def test_the_currency_recheck_predicts_against_the_configured_target(self) -> None:
        # ``feat-x`` conflicts with the integration branch and NOT with ``main``:
        # a gate still comparing against ``origin/main`` pushes a conflicting branch.
        ConfigSetting.objects.set_value("target_branch", _INTEGRATION)
        clone = _clone_with_conflicting_feature(self.tmp_path)
        ticket = self._ticket_on(clone)

        result, host = self._ship(ticket)

        assert result.ok is False
        assert "a.txt" in result.detail
        assert _INTEGRATION in result.detail
        host.create_pr.assert_not_called()

    def test_a_prefixed_ticket_override_is_remote_qualified_before_probing(self) -> None:
        # A bare ``release/1.2`` makes the fetch step ``git fetch release``, which
        # fails — and the gate then fails OPEN with no conflict probe at all.
        clone = _clone_with_conflicting_feature(self.tmp_path)
        ticket = self._ticket_on(clone)
        ticket.extra = {"target_branch": _INTEGRATION}
        ticket.save(update_fields=["extra"])

        result, host = self._ship(ticket)

        assert result.ok is False
        assert "a.txt" in result.detail
        host.create_pr.assert_not_called()
