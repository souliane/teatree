"""Overlay-registry writes are one read-modify-write against a SHARED document.

Every overlay's Slack fields live in the single ``overlays`` ``ConfigSetting`` row, so
an unserialized read-then-write lets a concurrent provisioning run's fields be erased
by a stale in-memory copy of the whole document. The control DB opens each ``atomic``
block with ``BEGIN IMMEDIATE``, so enclosing the pair is what serializes the writers.
"""

from unittest.mock import patch

from django.db import transaction
from django.test import TransactionTestCase

from teatree.cli.slack.app_resolve import read_overlay_field, read_overlay_registry, write_overlay_fields
from teatree.core.models import ConfigSetting


class TestRegistryWritesAreSerialized(TransactionTestCase):
    """``TransactionTestCase`` runs with NO ambient atomic block, so the guard is observable."""

    def test_the_read_and_the_write_share_one_transaction(self) -> None:
        observed: list[bool] = []
        real_set_value = ConfigSetting.objects.set_value

        def _record(key: str, value: object, *args: object, **kwargs: object) -> object:
            observed.append(transaction.get_connection().in_atomic_block)
            return real_set_value(key, value, *args, **kwargs)

        with patch.object(ConfigSetting.objects, "set_value", side_effect=_record):
            write_overlay_fields("acme", {"slack_app_id": "A1"})

        assert observed == [True]

    def test_an_existing_overlays_fields_survive_another_overlays_write(self) -> None:
        write_overlay_fields("acme", {"slack_app_id": "A1"})
        write_overlay_fields("widget", {"slack_app_id": "A2"})

        assert read_overlay_field("acme", "slack_app_id") == "A1"
        assert read_overlay_field("widget", "slack_app_id") == "A2"
        assert set(read_overlay_registry()) == {"acme", "widget"}
