"""SHA-bound, POSTED E2E evidence artifact for the mandatory-E2E gate (#1967).

``E2eMandatoryRun`` is the durable record that a green E2E run happened at a
specific reviewed tree AND was posted. The gate reads it by ``(ticket, head_sha,
result, posted_url≠"")`` so a green run only satisfies the gate at the SHA it was
recorded against and only when its evidence was posted (a recorded-but-unposted
run does not satisfy it). Re-recording the same (ticket, head_sha, spec) is an
update, not a duplicate (idempotent).
"""

from django.test import TestCase

from teatree.core.models import E2eMandatoryRun, Ticket

_SHA = "c" * 40
_OTHER_SHA = "d" * 40
_URL = "https://example.com/issues/1#note_99"


class TestRecordRun(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(issue_url="https://example.com/i/10")

    def test_record_green_posted_run(self) -> None:
        run = E2eMandatoryRun.record(
            ticket=self.ticket, head_sha=_SHA, spec="e2e/loan.spec.ts", result="green", posted_url=_URL
        )
        assert run.pk is not None
        assert run.result == "green"
        assert run.head_sha == _SHA
        assert run.posted_url == _URL

    def test_record_normalizes_sha(self) -> None:
        run = E2eMandatoryRun.record(ticket=self.ticket, head_sha="C" * 40, spec="x", result="green", posted_url=_URL)
        assert run.head_sha == "c" * 40

    def test_re_record_same_spec_and_sha_is_update_not_duplicate(self) -> None:
        E2eMandatoryRun.record(ticket=self.ticket, head_sha=_SHA, spec="e2e/loan.spec.ts", result="red")
        E2eMandatoryRun.record(
            ticket=self.ticket, head_sha=_SHA, spec="e2e/loan.spec.ts", result="green", posted_url=_URL
        )
        rows = E2eMandatoryRun.objects.filter(ticket=self.ticket, head_sha=_SHA, spec="e2e/loan.spec.ts")
        assert rows.count() == 1
        assert rows.first().result == "green"
        assert rows.first().posted_url == _URL


class TestHasGreenEvidence(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(issue_url="https://example.com/i/11")

    def test_green_posted_evidence_at_sha(self) -> None:
        E2eMandatoryRun.record(ticket=self.ticket, head_sha=_SHA, spec="x", result="green", posted_url=_URL)
        assert E2eMandatoryRun.has_green_evidence(self.ticket, _SHA) is True

    def test_green_but_unposted_is_not_evidence(self) -> None:
        # Recorded-but-unposted: green, but no posted comment URL -> the gate
        # is NOT satisfied (#1967 — evidence must be posted, not just recorded).
        E2eMandatoryRun.record(ticket=self.ticket, head_sha=_SHA, spec="x", result="green", posted_url="")
        assert E2eMandatoryRun.has_green_evidence(self.ticket, _SHA) is False

    def test_no_green_evidence_at_other_sha(self) -> None:
        E2eMandatoryRun.record(ticket=self.ticket, head_sha=_SHA, spec="x", result="green", posted_url=_URL)
        assert E2eMandatoryRun.has_green_evidence(self.ticket, _OTHER_SHA) is False

    def test_red_run_is_not_evidence(self) -> None:
        E2eMandatoryRun.record(ticket=self.ticket, head_sha=_SHA, spec="x", result="red", posted_url=_URL)
        assert E2eMandatoryRun.has_green_evidence(self.ticket, _SHA) is False

    def test_no_run_is_not_evidence(self) -> None:
        assert E2eMandatoryRun.has_green_evidence(self.ticket, _SHA) is False


class TestHasVisualVerification(TestCase):
    """The per-ticket (any-SHA) attestation the snapshot-baseline gate reads."""

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(issue_url="https://example.com/i/12")

    def test_green_posted_run_at_any_sha_is_attestation(self) -> None:
        E2eMandatoryRun.record(ticket=self.ticket, head_sha=_OTHER_SHA, spec="x", result="green", posted_url=_URL)
        assert E2eMandatoryRun.has_visual_verification(self.ticket) is True

    def test_green_but_unposted_is_not_attestation(self) -> None:
        E2eMandatoryRun.record(ticket=self.ticket, head_sha=_SHA, spec="x", result="green", posted_url="")
        assert E2eMandatoryRun.has_visual_verification(self.ticket) is False

    def test_red_run_is_not_attestation(self) -> None:
        E2eMandatoryRun.record(ticket=self.ticket, head_sha=_SHA, spec="x", result="red", posted_url=_URL)
        assert E2eMandatoryRun.has_visual_verification(self.ticket) is False

    def test_no_run_is_not_attestation(self) -> None:
        assert E2eMandatoryRun.has_visual_verification(self.ticket) is False

    def test_another_tickets_attestation_does_not_carry(self) -> None:
        other = Ticket.objects.create(issue_url="https://example.com/i/13")
        E2eMandatoryRun.record(ticket=other, head_sha=_SHA, spec="x", result="green", posted_url=_URL)
        assert E2eMandatoryRun.has_visual_verification(self.ticket) is False
