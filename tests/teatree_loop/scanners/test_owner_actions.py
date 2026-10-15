"""The global tick reports each answered action card it carried out, declined or refused."""

from unittest.mock import patch

import pytest
from django.db import OperationalError

from teatree.loop.scanners.owner_actions import OwnerActionScanner


def test_scanner_emits_one_result_per_answered_card() -> None:
    with patch(
        "teatree.loop.scanners.owner_actions.carry_out_owner_actions", return_value=["carried out 7", "refused 8"]
    ):
        signals = OwnerActionScanner().scan()
    assert [(signal.kind, signal.summary) for signal in signals] == [
        ("owner_action.result", "carried out 7"),
        ("owner_action.result", "refused 8"),
    ]


def test_scanner_surfaces_an_unreadable_action_queue() -> None:
    with (
        patch(
            "teatree.core.answering.owner_actions.DeferredQuestion.objects.filter",
            side_effect=OperationalError("unreadable"),
        ),
        pytest.raises(OperationalError, match="unreadable"),
    ):
        OwnerActionScanner().scan()
