import pytest
from django.contrib.auth import get_user_model
from django.db import DatabaseError, IntegrityError, transaction
from django.test import TestCase

from teatree.core.models import OperatorIdentity, Ticket
from teatree.core.models.operator_identity import canonical_operator_key, identity_key

_FORCE_INSERT_POSITIONALLY = (True,)
_FORCE_UPDATE_POSITIONALLY = (False, True)


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


class OperatorIdentitySaveFlagsTestCase(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.user = get_user_model().objects.create_user("alice")
        cls.identity = OperatorIdentity.objects.create(user=cls.user, platform="github", handle="alice")

    def test_force_insert_reaches_the_database_by_keyword_and_positionally(self) -> None:
        for label, save in (
            ("keyword", lambda: self.identity.save(force_insert=True)),
            ("positional", lambda: self.identity.save(*_FORCE_INSERT_POSITIONALLY)),
        ):
            with self.subTest(label), pytest.raises(IntegrityError), transaction.atomic():
                save()

    def test_force_update_reaches_the_database_by_keyword_and_positionally(self) -> None:
        ghost = OperatorIdentity(pk=self.identity.pk + 1000, user=self.user, platform="gitlab", handle="ghost")
        for label, save in (
            ("keyword", lambda: ghost.save(force_update=True)),
            ("positional", lambda: ghost.save(*_FORCE_UPDATE_POSITIONALLY)),
        ):
            with self.subTest(label), pytest.raises(DatabaseError, match="Forced update"), transaction.atomic():
                save()
        assert not OperatorIdentity.objects.filter(pk=ghost.pk).exists()


class TicketSaveFlagsTestCase(TestCase):
    def test_force_insert_reaches_the_database_by_keyword_and_positionally(self) -> None:
        ticket = Ticket.objects.create(overlay="teatree")
        for label, save in (
            ("keyword", lambda: ticket.save(force_insert=True)),
            ("positional", lambda: ticket.save(*_FORCE_INSERT_POSITIONALLY)),
        ):
            with self.subTest(label), pytest.raises(IntegrityError), transaction.atomic():
                save()

    def test_force_update_reaches_the_database_by_keyword_and_positionally(self) -> None:
        ghost = Ticket(pk=Ticket.objects.create(overlay="teatree").pk + 1000, overlay="teatree")
        for label, save in (
            ("keyword", lambda: ghost.save(force_update=True)),
            ("positional", lambda: ghost.save(*_FORCE_UPDATE_POSITIONALLY)),
        ):
            with self.subTest(label), pytest.raises(DatabaseError, match="Forced update"), transaction.atomic():
                save()
        assert not Ticket.objects.filter(pk=ghost.pk).exists()
