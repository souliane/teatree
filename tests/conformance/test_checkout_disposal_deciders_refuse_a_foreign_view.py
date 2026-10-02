"""No decider may authorise disposing of a checkout whose gitdir names a root absent here.

The fixture is the #3967 incident, built with real git: a checkout created by a
clone this context cannot reach, so its ``.git`` file records an absolute admin
dir that resolves to nothing here. git answers ``fatal: not a git repository``
there in the same words it uses for a directory that never held one — which is
the whole trap, and why the fixture-validity test below asserts that naive
reading is exactly what a bare probe produces.

Each entry below is a production decider whose answer can authorise removing or
re-creating a checkout. Every one of them must decline on this directory: absence
from THIS context's registry is a statement about the vantage point, never about
the disk.

A forward ratchet, not a repair. What it buys is that a decider added to the set,
or an existing one edited back onto "git could not resolve it, so it is dead",
goes red here rather than destroying a running agent's work.
"""

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.worktree.broken_checkout import BrokenCheckout, classify_broken_checkout
from teatree.core.worktree.checkout_disposal import disposal_refusal
from teatree.core.worktree.worktree_roots import CheckoutState, probe_checkout
from tests.teatree_core.cleanup._shared import _GIT, _clean_env, _run_git

_BRANCH = "3967-foreign-view"


class TestNoDeciderDisposesOfAForeignViewCheckout(TestCase):
    @pytest.fixture(autouse=True)
    def _foreign_view_checkout(self, tmp_path: Path) -> None:
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()
        self.acting_clone = self._clone(self.workspace / "repo")
        foreign_clone = self._clone(tmp_path / "another-context" / "repo")
        self.checkout = self.workspace / _BRANCH / "repo"
        _run_git("worktree", "add", "-q", "-b", _BRANCH, str(self.checkout), cwd=foreign_clone)
        # The pointer is written absolute by its creator, so removing the clone
        # leaves it naming a root reachable only from where it was written.
        shutil.rmtree(foreign_clone)

    def _clone(self, at: Path) -> Path:
        at.mkdir(parents=True)
        _run_git("init", "-q", "-b", "main", cwd=at)
        _run_git("config", "user.email", "t@t", cwd=at)
        _run_git("config", "user.name", "t", cwd=at)
        _run_git("commit", "-q", "--allow-empty", "-m", "initial", cwd=at)
        return at

    def _worktree_row(self) -> Worktree:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/3967")
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="repo",
            branch=_BRANCH,
            extra={"worktree_path": str(self.checkout), "clone_path": str(self.acting_clone)},
        )

    def _deciders(self) -> dict[str, Callable[[], bool]]:
        """Decider name → does it AUTHORISE removing or re-creating the checkout?"""
        return {
            "checkout_disposal.disposal_refusal": lambda: (
                disposal_refusal(self.checkout, clone=self.acting_clone) == ""
            ),
            "worktree_roots.probe_checkout": lambda: (
                probe_checkout(self.checkout, clone=self.acting_clone) is CheckoutState.NOT_A_CHECKOUT
            ),
            "broken_checkout.classify_broken_checkout": lambda: (
                classify_broken_checkout(self._worktree_row(), workspace=self.workspace).state
                is BrokenCheckout.RELEASABLE
            ),
        }

    def test_a_bare_git_probe_reads_the_fixture_as_no_repository(self) -> None:
        # The control: without it, every decider below could be declining for some
        # unrelated reason and the ratchet would pin nothing.
        probe = subprocess.run(
            [_GIT, "-C", str(self.checkout), "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            env=_clean_env(),
            check=False,
        )

        assert probe.returncode != 0
        assert "not a git repository" in probe.stderr.lower(), "fixture invalid — it must read as dead to git"

    def test_no_decider_authorises_disposal(self) -> None:
        authorising = [name for name, decides in self._deciders().items() if decides()]

        assert authorising == [], "these read an unresolvable pointer as proof of death"
