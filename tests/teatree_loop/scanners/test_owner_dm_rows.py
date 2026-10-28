"""The default owner lookup resolves each overlay's own backend, a blank overlay through the default one."""

from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from teatree.core.models import DmContext, PendingChatInjection
from teatree.loop.scanners import owner_dm_rows


class TestOwnerDmRows(TestCase):
    def test_a_blank_overlay_resolves_through_the_default_backend(self) -> None:
        for ts, user in (("1.0", "U_OWNER"), ("2.0", "U_COLLEAGUE")):
            PendingChatInjection.record(channel="D1", slack_ts=ts, text="hi", context=DmContext(user_id=user))

        with patch.object(
            owner_dm_rows, "messaging_from_overlay", return_value=SimpleNamespace(user_id="U_OWNER")
        ) as factory:
            kept = owner_dm_rows.owner_dm_rows(PendingChatInjection.loop_unreplied())

        assert list(kept.values_list("slack_ts", flat=True)) == ["1.0"]
        factory.assert_called_once_with(None)
