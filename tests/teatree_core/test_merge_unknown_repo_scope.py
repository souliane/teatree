"""The keystone merge refuses an UNKNOWN-repo target only when an overlay opts in.

SCOPE gate on the merge chokepoint: when some registered overlay declares
``owned_repos`` AND sets ``require_owned_repo_approval``, a CLEAR whose
``(host, slug)`` no opted-in overlay owns is held for the operator (escalated,
FSM untouched) unless a ``--human-authorized`` id is re-presented. The gate is
OPT-IN: with the flag off (the shipped default) every target merges normally.
An OWNED slug merges normally. An unresolvable host fails open.

The shipped ``t3-teatree`` overlay ships the gate INERT (flag False), so these
tests inject their own opted-in overlay set via ``get_all_overlays`` rather than
relying on the shipped flag.
"""

from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import MergeClear, Ticket
from teatree.core.overlay import OverlayBase, OverlayConfig

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SHA = "a" * 40
_MERGE_TICKET_PR = "teatree.core.management.commands._merge_keystone_commands.merge_ticket_pr"
_GET_ALL_OVERLAYS = "teatree.core.overlay_loader.get_all_overlays"


class _Overlay(OverlayBase):
    def __init__(self, *, owned: dict[str, list[str]], flag: bool) -> None:
        self.config = OverlayConfig()
        self.config.owned_repos = dict(owned)
        self.config.require_owned_repo_approval = flag

    def get_repos(self) -> list[str]:
        return []

    def get_provision_steps(self, worktree: object) -> list[object]:  # type: ignore[override]
        _ = worktree
        return []


def _opted_in(owned: dict[str, list[str]] | None = None) -> dict[str, OverlayBase]:
    return {"t3-teatree": _Overlay(owned=owned or {"github.com": ["souliane"]}, flag=True)}


def _gate_disabled() -> dict[str, OverlayBase]:
    return {"t3-teatree": _Overlay(owned={"github.com": ["souliane"]}, flag=False)}


def _ticket(slug: str, iid: int) -> Ticket:
    return Ticket.objects.create(issue_url=f"https://github.com/{slug}/issues/{iid}", short_description="t")


def _clear(ticket: Ticket, slug: str) -> MergeClear:
    return MergeClear.objects.create(
        ticket=ticket,
        pr_id=42,
        slug=slug,
        reviewed_sha=_SHA,
        reviewer_identity="cold-reviewer",
        gh_verify_result=MergeClear.VerifyResult.GREEN,
        blast_class=MergeClear.BlastClass.DOCS,
    )


def _merge(clear_id: int, **kwargs: str) -> dict:
    return cast("dict", call_command("ticket", "merge", str(clear_id), **kwargs))


class TestMergeUnknownRepoScope(TestCase):
    def test_unknown_repo_clear_is_escalated_when_an_overlay_opts_in(self) -> None:
        ticket = _ticket("randomuser/randomrepo", 1)
        clear = _clear(ticket, "randomuser/randomrepo")
        with patch(_GET_ALL_OVERLAYS, _opted_in), patch(_MERGE_TICKET_PR) as merge_pr:
            result = _merge(clear.pk)
        merge_pr.assert_not_called()
        assert result["merged"] is False
        assert result["escalated"] is True
        assert "randomuser/randomrepo" in result["error"]

    def test_unknown_repo_clear_merges_with_human_authorized(self) -> None:
        ticket = _ticket("randomuser/randomrepo", 2)
        clear = _clear(ticket, "randomuser/randomrepo")
        with patch(_GET_ALL_OVERLAYS, _opted_in), patch(_MERGE_TICKET_PR) as merge_pr:
            _merge(clear.pk, human_authorized="souliane")
        merge_pr.assert_called_once()

    def test_owned_repo_clear_is_not_held_by_the_scope_gate(self) -> None:
        ticket = _ticket("souliane/teatree", 3)
        clear = _clear(ticket, "souliane/teatree")
        with patch(_GET_ALL_OVERLAYS, _opted_in), patch(_MERGE_TICKET_PR) as merge_pr:
            _merge(clear.pk)
        merge_pr.assert_called_once()

    def test_gate_disabled_merges_an_unknown_repo(self) -> None:
        """HIGH 1 regression-gone: with the gate off (shipped default) an unknown repo merges."""
        ticket = _ticket("randomuser/randomrepo", 4)
        clear = _clear(ticket, "randomuser/randomrepo")
        with patch(_GET_ALL_OVERLAYS, _gate_disabled), patch(_MERGE_TICKET_PR) as merge_pr:
            _merge(clear.pk)
        merge_pr.assert_called_once()

    def test_gitlab_overlay_repo_merges_when_its_host_is_in_the_owned_list(self) -> None:
        """HIGH 1 opt-in path: a gitlab.com overlay repo merges once its host is declared owned."""
        owned = {"github.com": ["souliane"], "gitlab.com": ["some-private-namespace"]}
        ticket = Ticket.objects.create(
            issue_url="https://gitlab.com/some-private-namespace/overlay-repo/-/issues/1",
            short_description="t",
        )
        clear = _clear(ticket, "some-private-namespace/overlay-repo")
        with patch(_GET_ALL_OVERLAYS, lambda: _opted_in(owned)), patch(_MERGE_TICKET_PR) as merge_pr:
            _merge(clear.pk)
        merge_pr.assert_called_once()

    def test_gitlab_overlay_repo_is_held_when_its_host_is_not_owned(self) -> None:
        """Counterpart: the SAME gitlab repo is held while only github.com is declared owned."""
        ticket = Ticket.objects.create(
            issue_url="https://gitlab.com/some-private-namespace/overlay-repo/-/issues/2",
            short_description="t",
        )
        clear = _clear(ticket, "some-private-namespace/overlay-repo")
        with patch(_GET_ALL_OVERLAYS, _opted_in), patch(_MERGE_TICKET_PR) as merge_pr:
            result = _merge(clear.pk)
        merge_pr.assert_not_called()
        assert result["escalated"] is True

    def test_unresolvable_host_fails_open(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket, "souliane/teatree")
        with patch(_GET_ALL_OVERLAYS, _opted_in), patch(_MERGE_TICKET_PR) as merge_pr:
            _merge(clear.pk)
        merge_pr.assert_called_once()
