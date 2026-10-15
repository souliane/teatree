"""Commands that create owner action cards cannot be used to forge one."""

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import DeferredQuestion
from tests.void_merge_fixtures import PR_URL, landed_ticket, stub_forge


class TestOwnerActionCommands(TestCase):
    def test_request_for_merge_correction_records_an_owner_card(self) -> None:
        landed_ticket()
        with stub_forge():
            call_command("pr", "request-void", PR_URL, "The merge content disappeared.")
        row = DeferredQuestion.objects.get()
        assert row.is_pending
        assert row.evidence["action"]["kind"] == "void"

    def test_question_record_refuses_a_reserved_action_marker(self) -> None:
        with pytest.raises(SystemExit) as exc:
            call_command("questions", "record", "May I act?", "--dedupe-marker", "owner-action:attest:fake")
        assert exc.value.code == 2
        assert not DeferredQuestion.objects.exists()
