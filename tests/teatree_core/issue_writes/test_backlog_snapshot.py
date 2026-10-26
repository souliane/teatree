"""The snapshot token binds a judgment set to the backlog the first call returned."""

import pytest

from teatree.core.issue_writes.backlog_snapshot import appeared_since_snapshot, snapshot_token_for_urls

_ONE = "https://github.com/acme/widgets/issues/1"
_TWO = "https://github.com/acme/widgets/issues/2"
_THREE = "https://github.com/acme/widgets/issues/3"


def test_token_is_compact_and_ignores_order_and_duplicates() -> None:
    token = snapshot_token_for_urls([_TWO, _ONE])

    assert 0 < len(token) <= 32
    assert token == snapshot_token_for_urls([_ONE, _TWO, _ONE])


def test_token_differs_when_the_backlog_differs() -> None:
    assert snapshot_token_for_urls([_ONE]) != snapshot_token_for_urls([_ONE, _TWO])


def test_a_ticket_filed_after_the_snapshot_is_reported_sorted() -> None:
    token = snapshot_token_for_urls([_ONE])

    assert appeared_since_snapshot(token, judged_urls={_ONE}, live_urls=[_THREE, _ONE, _TWO]) == (_TWO, _THREE)


def test_a_ticket_closed_after_the_snapshot_is_not_reported() -> None:
    token = snapshot_token_for_urls([_ONE, _TWO])

    assert appeared_since_snapshot(token, judged_urls={_ONE, _TWO}, live_urls=[_ONE]) == ()


def test_judgments_that_do_not_name_the_snapshot_are_refused() -> None:
    token = snapshot_token_for_urls([_ONE, _TWO])

    with pytest.raises(ValueError, match="snapshot_token does not match"):
        appeared_since_snapshot(token, judged_urls={_ONE}, live_urls=[_ONE, _TWO])
