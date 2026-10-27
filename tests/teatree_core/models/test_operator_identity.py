import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from teatree.core.models import OperatorIdentity, Ticket
from teatree.core.models.operator_identity import canonical_operator_key, identity_key


class OperatorIdentityTestCase(TestCase):
    def test_forge_handles_are_casefolded_but_slack_ids_keep_case(self) -> None:
        assert identity_key("github", "ALIce") == "github:alice"
        assert identity_key("gitlab", "BoB") == "gitlab:bob"
        assert identity_key("slack", "UAbC") == "slack:UAbC"
        assert canonical_operator_key("GitHub:ALIce") == "github:alice"

    def test_unknown_platform_and_blank_handle_are_refused(self) -> None:
        for key in ("other:alice", "github:", "github"):
            with self.subTest(key=key), pytest.raises(ValueError, match="operator identity requires"):
                canonical_operator_key(key)

    def test_forge_external_id_is_unique_even_when_handle_changes(self) -> None:
        user = get_user_model().objects.create_user("alice")
        other = get_user_model().objects.create_user("bob")
        first = OperatorIdentity.objects.create(user=user, platform="github", handle="alice", external_id="123")
        assert first.key == "github:alice"
        with pytest.raises(IntegrityError), transaction.atomic():
            OperatorIdentity.objects.create(user=other, platform="github", handle="new-name", external_id="123")


class TicketRequesterTestCase(TestCase):
    def test_requester_is_blank_until_first_valid_identity_records_it(self) -> None:
        ticket = Ticket.objects.create(overlay="teatree")
        assert ticket.requested_by == ""
        assert ticket.record_requester("GitHub:ALIce") is True
        assert ticket.requested_by == "github:alice"
        assert ticket.record_requester("github:bob") is False
        ticket.refresh_from_db()
        assert ticket.requested_by == "github:alice"

    def test_a_stale_ticket_object_cannot_overwrite_the_first_requester(self) -> None:
        ticket = Ticket.objects.create(overlay="teatree")
        stale = Ticket.objects.get(pk=ticket.pk)
        assert ticket.record_requester("github:alice") is True
        assert stale.record_requester("github:bob") is False
        stale.refresh_from_db()
        assert stale.requested_by == "github:alice"

    def test_a_later_full_save_cannot_erase_a_requester_recorded_elsewhere(self) -> None:
        ticket = Ticket.objects.create(overlay="teatree")
        stale = Ticket.objects.get(pk=ticket.pk)
        ticket.record_requester("github:alice")
        stale.short_description = "updated"
        stale.save()
        ticket.refresh_from_db()
        assert ticket.requested_by == "github:alice"
