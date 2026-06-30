"""Anti-vacuity gate wired into the merge precondition path (#1829).

Extends the §17.4.3 merge gate with the anti-vacuity dimension: with
``require_anti_vacuity_attestation`` on, a merge is refused unless the CLEAR's
ticket carries a complete, SHA-bound anti-vacuity attestation. The attestation
binds to the merge-time live head, so a stale-SHA attestation (the bug present
on a later, un-re-attested revision) is treated as absent.

Only the unstoppable external (``gh``) is stubbed; the gate, CLEAR, FSM, and DB
writes are real. ``require_anti_vacuity_attestation`` is pinned per test so the
suite is deterministic regardless of the host config.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.config import UserSettings
from teatree.core.merge import MergePreconditionError, merge_ticket_pr
from teatree.core.models import MergeClear, Ticket
from tests.teatree_core.conftest import seed_merge_safe_verdict
from tests.teatree_core.test_merge_execution import _GhStub

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _skip_author_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    # #1773 public-repo author gate — exercised by test_merge_execution_author_gate;
    # these pre-date it and target other concerns, so it is a no-op here.
    monkeypatch.setattr("teatree.core.merge.execution.assert_public_repo_author_trusted", lambda **_: None)


_SHA = "a" * 40
_OTHER_SHA = "b" * 40


def _clear(ticket: Ticket) -> MergeClear:
    return MergeClear.objects.create(
        ticket=ticket,
        pr_id=859,
        slug="souliane/teatree",
        reviewed_sha=_SHA,
        reviewer_identity="cold-reviewer",
        gh_verify_result=MergeClear.VerifyResult.GREEN,
        blast_class=MergeClear.BlastClass.DOCS,
    )


@contextmanager
def _gate(*, required: bool) -> Iterator[None]:
    with patch(
        "teatree.core.gates.anti_vacuity_gate.get_effective_settings",
        return_value=UserSettings(require_anti_vacuity_attestation=required),
    ):
        yield


def _merge(clear: MergeClear) -> object:
    # Seed the #2829 sibling verdict the real ``clear`` path records (the gate
    # is downstream of the anti-vacuity check, so a refuse test is unaffected).
    seed_merge_safe_verdict(slug=clear.slug, pr_id=clear.pr_id, sha=clear.reviewed_sha)
    with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_GhStub()):
        return merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")


class TestMergeAntiVacuityGate(TestCase):
    def test_merge_refused_without_attestation_when_gate_on(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)
        with _gate(required=True), pytest.raises(MergePreconditionError, match="anti-vacuity"):
            _merge(clear)
        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW
        assert clear.consumed_at is None

    def test_merge_refused_with_stale_sha_attestation(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        ticket.record_anti_vacuity_attestation(_OTHER_SHA, "AC mapped", ["tests/x.py::test_y"])
        clear = _clear(ticket)
        with _gate(required=True), pytest.raises(MergePreconditionError, match="stale"):
            _merge(clear)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW

    def test_merge_allowed_with_bound_complete_attestation(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        ticket.record_anti_vacuity_attestation(_SHA, "AC1-3 mapped", ["tests/x.py::test_y"])
        clear = _clear(ticket)
        with _gate(required=True):
            _merge(clear)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED

    def test_merge_unaffected_when_gate_off(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)
        with _gate(required=False):
            _merge(clear)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED
