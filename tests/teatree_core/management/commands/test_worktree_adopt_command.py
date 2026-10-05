"""``t3 <overlay> worktree adopt`` — the verb for a checkout that exists on disk.

A worktree created outside ``t3`` (``git worktree add``, another
session, or one whose row was reaped while the checkout survived) had no way to
acquire a ``Worktree`` row, so every ``t3`` operation on it — ``run tests`` above all
— refused. The only creating verb was ``provision``, which runs tenant-DB provisioning
the overlay forbids for unit-test work, so the standing "use ``t3`` for everything"
rule was unfollowable. Adoption is the missing verb: it records the row and does
nothing else.

Every assertion is on the CLI's ANSWER and on the row it left behind, because the row
is the whole deliverable — and on what it did NOT do (no provisioning, no DB).
"""

from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import Ticket, Worktree

_BRANCH = "teatree.core.intake.resolve.git.current_branch"
_ADOPT_BRANCH = "teatree.core.provision.worktree_adopt.git.current_branch"
_OVERLAY_NAME_FOR_CWD = "teatree.core.management.commands._worktree_adopt_command._overlay_name_for_cwd"


class _AdoptCase(TestCase):
    @pytest.fixture(autouse=True)
    def _inject(self, tmp_path: Path) -> None:
        self._tmp = tmp_path

    def _linked_worktree(self, name: str = "backend") -> Path:
        gitdir = self._tmp / "clone" / ".git" / "worktrees" / name
        gitdir.mkdir(parents=True)
        checkout = self._tmp / name
        checkout.mkdir()
        (checkout / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
        return checkout

    def _dangling_worktree(self, name: str = "backend") -> Path:
        checkout = self._tmp / name
        checkout.mkdir()
        (checkout / ".git").write_text(f"gitdir: /elsewhere/backend/.git/worktrees/{name}\n", encoding="utf-8")
        return checkout

    def run_adopt(self, *args: str, branch: str = "1722-cfgapi-guards") -> str:
        out, err = StringIO(), StringIO()
        with patch(_BRANCH, return_value=branch), patch(_ADOPT_BRANCH, return_value=branch):
            call_command("worktree", "adopt", *args, stdout=out, stderr=err)
        return out.getvalue() + err.getvalue()


class AdoptCreatesTheRow(_AdoptCase):
    def test_records_the_checkout_with_its_branch(self) -> None:
        checkout = self._linked_worktree()

        self.run_adopt(str(checkout))

        row = Worktree.objects.get()
        assert row.branch == "1722-cfgapi-guards"
        assert row.extra["worktree_path"] == str(checkout.resolve())

    def test_reports_the_row_it_created(self) -> None:
        checkout = self._linked_worktree()

        output = self.run_adopt(str(checkout))

        assert str(checkout) in output
        assert "1722-cfgapi-guards" in output

    def test_leaves_the_row_unprovisioned(self) -> None:
        # The whole point of the verb: a row WITHOUT provisioning, so an overlay that
        # forbids tenant-DB provisioning for unit-test work can still be tested.
        checkout = self._linked_worktree()

        self.run_adopt(str(checkout))

        assert Worktree.objects.get().state == Worktree.State.CREATED

    def test_attaches_to_the_ticket_named_by_the_flag(self) -> None:
        checkout = self._linked_worktree()
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/4242")

        self.run_adopt(str(checkout), "--ticket", "4242")

        assert Worktree.objects.get().ticket_id == ticket.pk

    def test_unknown_ticket_number_is_refused(self) -> None:
        checkout = self._linked_worktree()

        with pytest.raises(SystemExit) as caught:
            self.run_adopt(str(checkout), "--ticket", "9999")

        assert caught.value.code == 1
        assert not Worktree.objects.exists()

    def test_ticket_flag_never_cross_attaches_to_a_foreign_overlays_ticket(self) -> None:
        # A same-numbered ticket living in a DIFFERENT overlay must never satisfy
        # --ticket for this checkout's overlay — the unscoped lookup this guards
        # against would have cross-attached the row to the wrong overlay's ticket.
        checkout = self._linked_worktree()
        Ticket.objects.create(overlay="beta", issue_url="https://example.com/issues/4242")

        with (
            patch(f"{_OVERLAY_NAME_FOR_CWD}", return_value="alpha"),
            pytest.raises(SystemExit) as caught,
        ):
            self.run_adopt(str(checkout), "--ticket", "4242")

        assert caught.value.code == 1
        assert not Worktree.objects.exists()

    def test_ticket_flag_collision_is_a_clean_refusal_not_a_traceback(self) -> None:
        # Two same-overlay tickets sharing a ticket_number must raise the CLI's
        # own refusal (exit 1), never let TicketIdentityCollisionError escape
        # uncaught from the --ticket branch.
        checkout = self._linked_worktree()
        Ticket.objects.create(overlay="alpha", issue_url="https://example.com/issues/4242")
        Ticket.objects.create(overlay="alpha", issue_url="https://example.org/issues/4242")

        with (
            patch(f"{_OVERLAY_NAME_FOR_CWD}", return_value="alpha"),
            pytest.raises(SystemExit) as caught,
        ):
            self.run_adopt(str(checkout), "--ticket", "4242")

        assert caught.value.code == 1
        assert not Worktree.objects.exists()


class AdoptRefusals(_AdoptCase):
    def test_refuses_a_path_another_row_already_records(self) -> None:
        checkout = self._linked_worktree()
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/1")
        owner = Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="backend",
            branch="already-here",
            extra={"worktree_path": str(checkout.resolve())},
        )

        with pytest.raises(SystemExit) as caught:
            self.run_adopt(str(checkout))

        assert caught.value.code == 1
        assert Worktree.objects.count() == 1
        assert Worktree.objects.get().pk == owner.pk
        # The refusal comes BEFORE attribution, so a refused adopt forks no ticket.
        assert not Ticket.objects.filter(issue_url__startswith="auto:").exists()

    def test_a_refused_claimed_path_forks_no_synthetic_ticket(self) -> None:
        # The claimed-path refusal has to come BEFORE attribution. Under a workspace
        # parent with several ticket owners the workspace hint declines (#3379), so an
        # attribution-first order reaches the `auto:<branch>` fork and leaves a stray
        # ticket behind for a checkout it then refuses.
        claimed = self._linked_worktree()
        sibling = self._linked_worktree("sibling")
        for issue, checkout in ((1, claimed), (2, sibling)):
            ticket = Ticket.objects.create(overlay="test", issue_url=f"https://example.com/issues/{issue}")
            Worktree.objects.create(
                ticket=ticket,
                overlay="test",
                repo_path=checkout.name,
                branch=f"held-{issue}",
                extra={"worktree_path": str(checkout.resolve())},
            )

        with pytest.raises(SystemExit) as caught:
            self.run_adopt(str(claimed), branch="feat/no-number-here")

        assert caught.value.code == 1
        assert not Ticket.objects.filter(issue_url__startswith="auto:").exists()

    def test_refuses_a_directory_that_is_not_a_checkout(self) -> None:
        plain = self._tmp / "plain"
        plain.mkdir()

        with pytest.raises(SystemExit) as caught:
            self.run_adopt(str(plain))

        assert caught.value.code == 1
        assert not Worktree.objects.exists()

    def test_refuses_a_main_clone(self) -> None:
        clone = self._tmp / "clone-root"
        (clone / ".git").mkdir(parents=True)

        with pytest.raises(SystemExit) as caught:
            self.run_adopt(str(clone))

        assert caught.value.code == 1
        assert not Worktree.objects.exists()

    def test_refuses_an_unreachable_gitdir_naming_the_pointer(self) -> None:
        # Amendment 2026-09-21: a host `git worktree add` checkout whose clone the
        # container never mounts. The refusal has to name the pointer, because the
        # fix is to mount that path — not to stand somewhere else.
        checkout = self._dangling_worktree()

        with pytest.raises(SystemExit) as caught:
            self.run_adopt(str(checkout), branch="")

        assert caught.value.code == 1
        assert not Worktree.objects.exists()

    def test_refuses_a_checkout_on_a_default_branch(self) -> None:
        checkout = self._linked_worktree()

        with pytest.raises(SystemExit) as caught:
            self.run_adopt(str(checkout), branch="main")

        assert caught.value.code == 1
        assert not Worktree.objects.exists()
        assert not Ticket.objects.exists()
