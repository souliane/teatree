"""Only the rows an overlay's own configured owner sent are that overlay's owner work."""

from collections.abc import Callable
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase

from teatree.core.models import DmContext, OperatorIdentity, PendingChatInjection
from teatree.loop.owner_identity import (
    configured_owner_id,
    is_the_owner,
    operator_rows,
    owner_rows,
    sent_by_operator,
    sent_by_owner,
)


def _owners(**by_overlay: str) -> Callable[[str], object]:
    return lambda overlay: SimpleNamespace(user_id=by_overlay[overlay]) if overlay in by_overlay else None


def _record(overlay: str, ts: str, user: str) -> None:
    PendingChatInjection.record(channel="D1", slack_ts=ts, text="hi", overlay=overlay, context=DmContext(user_id=user))


class TestOwnerRows(TestCase):
    def setUp(self) -> None:
        _record("a", "1.0", "UA")
        _record("a", "2.0", "UB")
        _record("b", "3.0", "UB")
        _record("b", "4.0", "UC")
        _record("a", "5.0", "")

    @staticmethod
    def _kept(resolver: Callable[[str], object]) -> list[str]:
        return list(owner_rows(PendingChatInjection.loop_unreplied(), resolver).values_list("slack_ts", flat=True))

    def test_each_overlay_keeps_only_its_own_owners_rows(self) -> None:
        assert self._kept(_owners(a="UA", b="UB")) == ["1.0", "3.0"]

    def test_an_overlay_without_a_readable_owner_keeps_nothing(self) -> None:
        assert self._kept(_owners(a="UA")) == ["1.0"]
        assert self._kept(_owners(a="", b="UB")) == ["3.0"]

    def test_nothing_is_kept_when_no_owner_resolves(self) -> None:
        assert self._kept(lambda _overlay: None) == []

    def test_an_overlay_whose_backend_raises_cannot_hide_another_overlays_rows(self) -> None:
        def backend_for(overlay: str) -> object:
            if overlay == "a":
                msg = "backend unavailable"
                raise RuntimeError(msg)
            return SimpleNamespace(user_id="UB")

        with self.assertLogs("teatree.loop.owner_identity", level="ERROR"):
            assert self._kept(backend_for) == ["3.0"]


@pytest.mark.parametrize(
    ("user", "owner", "expected"),
    [("U1", "U1", True), ("U2", "U1", False), ("U1", "", False), ("", "", False), (None, "U1", False)],
)
def test_only_an_exact_nonempty_match_is_the_owner(user: object, owner: str, *, expected: bool) -> None:
    assert is_the_owner(user, owner) is expected


def test_an_event_without_a_sender_is_not_sent_by_the_owner() -> None:
    assert sent_by_owner({"user": "U1"}, "U1")
    assert not sent_by_owner({"text": "no sender"}, "U1")


def test_a_backend_without_an_owner_id_has_no_configured_owner() -> None:
    assert configured_owner_id(SimpleNamespace()) == ""
    assert configured_owner_id(None) == ""
    assert configured_owner_id(SimpleNamespace(user_id="U1")) == "U1"


class TestOperatorSenders(TestCase):
    def setUp(self) -> None:
        operator = get_user_model().objects.create_superuser("alice", email="", password="unused")
        OperatorIdentity.objects.create(user=operator, platform="slack", handle="U-OP")
        viewer = get_user_model().objects.create_user("viewer", password="unused")
        OperatorIdentity.objects.create(user=viewer, platform="slack", handle="U-VIEWER")

    def test_the_owner_and_a_registered_superuser_are_operators_and_nobody_else_is(self) -> None:
        assert sent_by_operator({"user": "U-OWNER"}, "U-OWNER")
        assert sent_by_operator({"user": "U-OP"}, "U-OWNER")
        assert not sent_by_operator({"user": "U-VIEWER"}, "U-OWNER")
        assert not sent_by_operator({"user": "U-STRANGER"}, "U-OWNER")
        assert not sent_by_operator({"text": "no sender"}, "U-OWNER")

    def test_operator_rows_keep_the_owners_and_registered_operators_rows(self) -> None:
        for ts, user in (("1.0", "U-OWNER"), ("2.0", "U-OP"), ("3.0", "U-VIEWER"), ("4.0", "U-STRANGER")):
            _record("a", ts, user)

        kept = operator_rows(PendingChatInjection.loop_unreplied(), _owners(a="U-OWNER"))

        assert sorted(kept.values_list("slack_ts", flat=True)) == ["1.0", "2.0"]
