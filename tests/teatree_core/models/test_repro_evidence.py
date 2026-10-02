"""``ReproEvidence`` — the guarded RED->GREEN reproduction factory (#118).

The anti-fabrication enforcement lives in the ``record_red`` / ``record_green``
factories: a passing command is not a failing RED, a still-failing command is not
a GREEN, and a RED captured against the same tree as (or a non-ancestor of) the
GREEN is a provenance bypass. The ancestry verdict is frozen into
``provenance_ok`` at record time so ``has_valid_repro`` is a pure DB read that a
hand-crafted row cannot spoof.
"""

import pytest
from django.test import TestCase

from teatree.core.models import HarnessRun, ReproEvidence, ReproEvidenceError, ReproEvidenceManager, Ticket

_SHA_RED = "a" * 40
_SHA_GREEN = "b" * 40
_CMD = "uv run pytest tests/x.py::test_bug"


def _fix_ticket() -> Ticket:
    return Ticket.objects.create(overlay="acme", kind=Ticket.Kind.FIX)


def _record_red(ticket: Ticket, *, exit_code: int = 1, head_sha: str = _SHA_RED, command: str = _CMD) -> ReproEvidence:
    run = HarnessRun(head_sha=head_sha, exit_code=exit_code, output="boom traceback")
    return ReproEvidence.record_red(ticket=ticket, command=command, run=run)


class TestRecordRed(TestCase):
    def test_failing_command_records_a_red_row_with_provenance_unproven(self) -> None:
        ticket = _fix_ticket()
        row = _record_red(ticket)
        assert row.red_head_sha == _SHA_RED
        assert row.red_exit_code == 1
        assert row.provenance_ok is False
        assert row.green_head_sha == ""

    def test_passing_command_is_refused(self) -> None:
        # RED-4: a fabricated "red" that actually exits 0 is not a failing repro.
        with pytest.raises(ReproEvidenceError) as exc:
            _record_red(_fix_ticket(), exit_code=0)
        assert "exited 0" in str(exc.value)

    def test_abbreviated_head_sha_is_refused(self) -> None:
        with pytest.raises(ReproEvidenceError):
            _record_red(_fix_ticket(), head_sha="abc123")

    def test_rerunning_the_same_command_updates_in_place(self) -> None:
        ticket = _fix_ticket()
        first = _record_red(ticket, exit_code=1)
        second = _record_red(ticket, exit_code=2)
        assert first.pk == second.pk
        assert ReproEvidence.objects.filter(ticket=ticket).count() == 1
        assert second.red_exit_code == 2

    def test_re_red_after_green_is_refused_as_tamper(self) -> None:
        ticket = _fix_ticket()
        _record_red(ticket)
        ReproEvidence.record_green(
            ticket=ticket,
            command=_CMD,
            run=HarnessRun(head_sha=_SHA_GREEN, exit_code=0, output="ok"),
            red_is_ancestor=True,
        )
        with pytest.raises(ReproEvidenceError) as exc:
            _record_red(ticket)
        assert "tamper" in str(exc.value)


class TestRecordGreen(TestCase):
    def _green(
        self, ticket: Ticket, *, exit_code: int = 0, head_sha: str = _SHA_GREEN, red_is_ancestor: bool = True
    ) -> ReproEvidence:
        run = HarnessRun(head_sha=head_sha, exit_code=exit_code, output="ok")
        return ReproEvidence.record_green(ticket=ticket, command=_CMD, run=run, red_is_ancestor=red_is_ancestor)

    def test_valid_pair_freezes_provenance_ok_true(self) -> None:
        ticket = _fix_ticket()
        _record_red(ticket)
        row = self._green(ticket)
        assert row.provenance_ok is True
        assert row.green_head_sha == _SHA_GREEN
        assert row.green_exit_code == 0

    def test_still_failing_command_is_refused(self) -> None:
        # RED-5: a green that did not actually pass — the fix did not fix it.
        ticket = _fix_ticket()
        _record_red(ticket)
        with pytest.raises(ReproEvidenceError) as exc:
            self._green(ticket, exit_code=1)
        assert "did not fix it" in str(exc.value)

    def test_no_matching_red_is_refused(self) -> None:
        with pytest.raises(ReproEvidenceError) as exc:
            self._green(_fix_ticket())
        assert "no matching RED" in str(exc.value)

    def test_red_equal_to_green_tree_is_refused(self) -> None:
        # RED-2a: RED captured against the SAME tree as GREEN — captured with the fix.
        ticket = _fix_ticket()
        _record_red(ticket, head_sha=_SHA_GREEN)
        with pytest.raises(ReproEvidenceError) as exc:
            self._green(ticket, red_is_ancestor=True)
        assert "SAME tree" in str(exc.value)

    def test_non_ancestor_red_is_refused_as_provenance_bypass(self) -> None:
        # RED-2b: the RED tree is not a proper ancestor of the GREEN tree.
        ticket = _fix_ticket()
        _record_red(ticket)
        with pytest.raises(ReproEvidenceError) as exc:
            self._green(ticket, red_is_ancestor=False)
        assert "ancestor" in str(exc.value)
        row = ReproEvidence.objects.get(ticket=ticket)
        assert row.provenance_ok is False  # no valid pair was written

    def test_abbreviated_green_head_sha_is_refused(self) -> None:
        ticket = _fix_ticket()
        _record_red(ticket)
        with pytest.raises(ReproEvidenceError):
            self._green(ticket, head_sha="abc123")


class TestStr(TestCase):
    def test_str_renders_red_and_pending_green(self) -> None:
        ticket = _fix_ticket()
        rendered = str(_record_red(ticket))
        assert "aaaaaaaa->—" in rendered
        assert "ok=False" in rendered


class TestHasValidRepro(TestCase):
    def test_valid_pair_satisfies(self) -> None:
        ticket = _fix_ticket()
        _record_red(ticket)
        ReproEvidence.record_green(
            ticket=ticket,
            command=_CMD,
            run=HarnessRun(head_sha=_SHA_GREEN, exit_code=0, output="ok"),
            red_is_ancestor=True,
        )
        assert ReproEvidence.objects.has_valid_repro(ticket) is True

    def test_red_only_does_not_satisfy(self) -> None:
        # RED-6: a partial (red-only) row was never shown to go green.
        ticket = _fix_ticket()
        _record_red(ticket)
        assert ReproEvidence.objects.has_valid_repro(ticket) is False

    def test_hand_crafted_provenance_false_row_does_not_satisfy(self) -> None:
        # RED-3 (manager layer): a directly-written row with both SHAs set but
        # provenance_ok=False must NOT satisfy — the frozen ancestry proof is the
        # gate's only trust anchor, unspoofable by bypassing the factory.
        ticket = _fix_ticket()
        ReproEvidence.objects.create(
            ticket=ticket,
            command=_CMD,
            command_fingerprint="deadbeef",
            red_head_sha=_SHA_RED,
            red_exit_code=1,
            red_output_digest="x",
            green_head_sha=_SHA_GREEN,
            green_exit_code=0,
            green_output_digest="y",
            provenance_ok=False,
        )
        assert ReproEvidence.objects.has_valid_repro(ticket) is False

    def test_red_sha_for_returns_recorded_sha_or_blank(self) -> None:
        ticket = _fix_ticket()
        assert ReproEvidence.objects.red_sha_for(ticket, _CMD) == ""
        _record_red(ticket)
        assert ReproEvidence.objects.red_sha_for(ticket, _CMD) == _SHA_RED

    def test_default_manager_is_the_custom_manager(self) -> None:
        assert isinstance(ReproEvidence.objects, ReproEvidenceManager)
