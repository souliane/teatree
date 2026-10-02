# test-path: cross-cutting
"""The worktree ROOT comes from the TICKET, not from the provisioning process.

``worktree_root()`` resolved the overlay segment from process-ambient state. Via
the CLI ``T3_OVERLAY_NAME`` is set and the segment appears; in the container the
var is unset, ``WORKDIR`` has no ``manage.py`` ancestor and two overlays are
registered, so the ambient name resolves to ``""`` and the segment vanishes. Two
provision runs of ONE ticket then disagree about where its worktrees live, and a
ticket whose repos are split across two roots cannot resolve its own siblings —
the generated stack silently loses services while provisioning reports success.

Real git under ``tmp_path`` and a sandboxed ``HOME``: the resolver's default tier
is what is under test, so nothing here may patch ``worktree_root`` itself.
"""

import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.runners import WorktreeProvisioner
from teatree.utils.env import patched_environ
from tests._git_repo import make_git_repo, run_git
from tests.teatree_core.conftest import CommandOverlay

_MOCK_OVERLAY = {"test": CommandOverlay()}
_BRANCH = "ac-111-split"


class TestProvisionRootComesFromTheTicket(TestCase):
    def setUp(self) -> None:
        super().setUp()
        sandbox = Path(tempfile.mkdtemp(prefix="teatree-provision-root-"))
        self.addCleanup(lambda: shutil.rmtree(sandbox, ignore_errors=True))
        self.home = sandbox / "home"
        self.clones = self.home / "workspace"
        self.clones.mkdir(parents=True)
        for repo in ("repo-a", "repo-b"):
            make_git_repo(self.clones / repo)
        self.enterContext(patch.object(Path, "home", return_value=self.home))
        self.enterContext(patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY))
        # `git pull --ff-only` against a clone with no remote is a network call
        # this fixture has nothing to serve; the provision path ignores its result.
        self.enterContext(patch("teatree.core.runners.provision.git.pull_ff_only", return_value=True))

    def _ticket(self, repos: list[str]) -> Ticket:
        ticket, _ = Ticket.objects.update_or_create(
            issue_url="https://example.com/issues/111",
            defaults={"overlay": "test", "repos": repos, "extra": {"branch": _BRANCH}},
        )
        return ticket

    def _provision_from_the_cli_venue(self, ticket: Ticket) -> None:
        with patched_environ({"HOME": str(self.home), "T3_OVERLAY_NAME": "test"}, remove=("T3_WORKSPACE_DIR",)):
            assert WorktreeProvisioner(ticket).run().ok

    def _provision_from_the_container_venue(self, ticket: Ticket) -> None:
        # The container resolves NO ambient overlay: no env var, no cwd discovery,
        # and more than one registered overlay so the single-overlay fallback
        # never fires.
        with (
            patched_environ({"HOME": str(self.home)}, remove=("T3_WORKSPACE_DIR", "T3_OVERLAY_NAME")),
            patch("teatree.config.resolution._active_overlay_entry", return_value=None),
        ):
            assert WorktreeProvisioner(ticket).run().ok

    def _recorded_dirs(self, ticket: Ticket) -> set[Path]:
        return {Path((wt.extra or {})["worktree_path"]).parent for wt in Worktree.objects.for_ticket(ticket)}

    def test_two_provision_runs_land_both_repos_in_one_dir_when_the_ambient_overlay_is_unresolvable(self) -> None:
        ticket = self._ticket(["repo-a"])
        self._provision_from_the_cli_venue(ticket)
        cli_dir = self._recorded_dirs(ticket).pop()

        # The ticket's only checkout is torn down (an ordinary `worktree teardown`),
        # so the second run has no materialised sibling to co-locate against and
        # falls back to the ROOT — the resolution this fix moves off ambient state.
        run_git(self.clones / "repo-a", "worktree", "remove", "--force", str(cli_dir / "repo-a"))
        ticket.repos = ["repo-a", "repo-b"]
        ticket.save(update_fields=["repos"])

        self._provision_from_the_container_venue(ticket)

        assert self._recorded_dirs(ticket) == {cli_dir}
        assert cli_dir == self.home / "workspace" / "t3-workspaces" / "test" / _BRANCH

    def test_a_repo_added_from_the_container_joins_the_existing_workspace(self) -> None:
        # The co-location path (an existing sibling pins the dir) is untouched by
        # the root change — the control that proves the assertion above is about
        # the ROOT and not about co-location doing the work.
        ticket = self._ticket(["repo-a"])
        self._provision_from_the_cli_venue(ticket)
        ticket.repos = ["repo-a", "repo-b"]
        ticket.save(update_fields=["repos"])

        self._provision_from_the_container_venue(ticket)

        assert len(self._recorded_dirs(ticket)) == 1
        assert (self.home / "workspace" / "t3-workspaces" / "test" / _BRANCH / "repo-b").is_dir()
