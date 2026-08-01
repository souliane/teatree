"""Proactive rotation + atomic persistence of the Slack app-config token pair."""

import datetime as dt
from unittest.mock import patch

import httpx
import pytest

from teatree.cli.slack.config_token import (
    CONFIG_TOKEN_LIFETIME,
    ROTATE_AFTER,
    ConfigTokenStore,
    RotationOutcome,
    SlackConfigTokenPersistError,
    ensure_fresh_config_token,
)
from teatree.cli.slack.manifest import SlackManifestError

_NOW = dt.datetime(2026, 7, 28, 12, 0, tzinfo=dt.UTC)
_STORE = ConfigTokenStore()


class _FakePassStore:
    """An in-memory ``pass`` double that can be told to refuse specific keys."""

    def __init__(self, initial: dict[str, str] | None = None, *, refuse: set[str] | None = None) -> None:
        self.values = dict(initial or {})
        self.refuse = refuse or set()
        self.writes: list[str] = []

    def read(self, key: str) -> str:
        return self.values.get(key, "")

    def write(self, key: str, value: str) -> bool:
        self.writes.append(key)
        if key in self.refuse:
            return False
        self.values[key] = value
        return True


def _seeded(*, issued_at: dt.datetime | None, refuse: set[str] | None = None) -> _FakePassStore:
    values = {_STORE.access_key: "xoxe.xoxp-OLD", _STORE.refresh_key: "xoxe-OLDR"}
    if issued_at is not None:
        values[_STORE.issued_at_key] = issued_at.isoformat()
    return _FakePassStore(values, refuse=refuse)


def _run(store: _FakePassStore, rotate: object, *, now: dt.datetime = _NOW):
    with (
        patch("teatree.cli.slack.config_token.read_pass", side_effect=store.read),
        patch("teatree.cli.slack.config_token.write_pass", side_effect=store.write),
        patch("teatree.cli.slack.config_token.rotate_config_token", side_effect=rotate),
    ):
        return ensure_fresh_config_token(now=now)


def _issues_new_pair(*, refresh_token: str) -> tuple[str, str]:
    return "xoxe.xoxp-NEW", "xoxe-NEWR"


class TestRotatesOnAge:
    def test_fresh_pair_is_left_alone_without_calling_slack(self) -> None:
        store = _seeded(issued_at=_NOW - dt.timedelta(minutes=30))

        def never_called(*, refresh_token: str) -> tuple[str, str]:
            return pytest.fail("a fresh pair must not be rotated")

        report = _run(store, never_called)

        assert report.outcome is RotationOutcome.FRESH
        assert store.writes == []

    def test_pair_older_than_the_threshold_is_rotated_and_persisted(self) -> None:
        store = _seeded(issued_at=_NOW - ROTATE_AFTER - dt.timedelta(minutes=1))

        report = _run(store, _issues_new_pair)

        assert report.outcome is RotationOutcome.ROTATED
        assert store.values[_STORE.access_key] == "xoxe.xoxp-NEW"
        assert store.values[_STORE.refresh_key] == "xoxe-NEWR"
        assert store.values[_STORE.issued_at_key] == _NOW.isoformat()
        # The age reported is the one the pair HAD, read before the new issue
        # time was written over it — not the 0.0h the fresh stamp would give.
        assert "4.0h" in report.detail

    def test_rotation_happens_well_inside_slacks_twelve_hour_expiry(self) -> None:
        """The whole point: rotate on age, with hours of margin left, never at expiry.

        Pinned as a ratio so the margin survives either constant being re-tuned:
        several consecutive missed ticks must still land inside the window.
        """
        assert ROTATE_AFTER * 2 < CONFIG_TOKEN_LIFETIME

    def test_pair_with_no_recorded_issue_time_is_rotated_immediately(self) -> None:
        """An unknown age must never be assumed fresh — that assumption is what let a pair expire."""
        store = _seeded(issued_at=None)

        report = _run(store, _issues_new_pair)

        assert report.outcome is RotationOutcome.ROTATED
        assert store.values[_STORE.issued_at_key] == _NOW.isoformat()

    def test_unparseable_issue_time_is_treated_as_unknown_age(self) -> None:
        store = _seeded(issued_at=None)
        store.values[_STORE.issued_at_key] = "not-a-timestamp"

        assert _run(store, _issues_new_pair).outcome is RotationOutcome.ROTATED

    def test_unseeded_box_reports_not_configured_without_calling_slack(self) -> None:
        store = _FakePassStore()

        def never_called(*, refresh_token: str) -> tuple[str, str]:
            return pytest.fail("nothing to rotate on an unseeded box")

        report = _run(store, never_called)

        assert report.outcome is RotationOutcome.NOT_CONFIGURED
        assert store.writes == []


class TestPersistenceIsAtomic:
    def test_rotate_succeeds_but_write_fails_raises_instead_of_losing_the_pair(self) -> None:
        """Slack already spent the old pair, so a lost write must be a named failure, never silence."""
        store = _seeded(issued_at=None, refuse={_STORE.refresh_key})

        with pytest.raises(SlackConfigTokenPersistError) as exc_info:
            _run(store, _issues_new_pair)

        assert _STORE.refresh_key in str(exc_info.value)
        assert "re-minted" in str(exc_info.value)

    def test_a_write_that_cannot_be_read_back_is_a_failure(self) -> None:
        """A `pass insert` reporting success while storing nothing must not pass for persistence."""
        store = _seeded(issued_at=None)
        store.write = lambda key, value: True  # type: ignore[method-assign] — accepts, stores nothing

        with pytest.raises(SlackConfigTokenPersistError):
            _run(store, _issues_new_pair)

    def test_the_refresh_half_is_written_before_the_access_half(self) -> None:
        """Crash-order matters: a stored refresh token can mint another pair, a stored access token cannot."""
        store = _seeded(issued_at=None)

        _run(store, _issues_new_pair)

        assert store.writes.index(_STORE.refresh_key) < store.writes.index(_STORE.access_key)

    def test_a_failed_write_is_retried_but_the_rotation_never_is(self) -> None:
        """Re-rotating to fix a storage fault would spend a second refresh token."""
        store = _seeded(issued_at=None, refuse={_STORE.access_key})
        rotations: list[str] = []

        def counting_rotate(*, refresh_token: str) -> tuple[str, str]:
            rotations.append(refresh_token)
            return "xoxe.xoxp-NEW", "xoxe-NEWR"

        with pytest.raises(SlackConfigTokenPersistError):
            _run(store, counting_rotate)

        assert len(rotations) == 1
        assert store.writes.count(_STORE.access_key) > 1


class TestSlackRefusalIsReportedNotRaised:
    @pytest.mark.parametrize(
        "failure",
        [SlackManifestError("invalid_auth"), httpx.ConnectError("no route to host")],
    )
    def test_a_dead_refresh_token_reports_unrecoverable(self, failure: Exception) -> None:
        """The app-config token gates only setup-time manifest edits — it must never break a tick."""
        store = _seeded(issued_at=None)

        def refusing(*, refresh_token: str) -> tuple[str, str]:
            raise failure

        report = _run(store, refusing)

        assert report.outcome is RotationOutcome.UNRECOVERABLE
        assert "api.slack.com/apps" in report.detail
        assert store.values[_STORE.refresh_key] == "xoxe-OLDR"


class TestIssueTimeIsRecorded:
    def test_age_is_knowable_without_calling_slack(self) -> None:
        store = _seeded(issued_at=_NOW - dt.timedelta(hours=3))

        with patch("teatree.cli.slack.config_token.read_pass", side_effect=store.read):
            assert _STORE.age(now=_NOW) == dt.timedelta(hours=3)

    def test_a_naive_stored_timestamp_is_read_as_utc(self) -> None:
        store = _seeded(issued_at=None)
        store.values[_STORE.issued_at_key] = _NOW.replace(tzinfo=None).isoformat()

        with patch("teatree.cli.slack.config_token.read_pass", side_effect=store.read):
            assert _STORE.age(now=_NOW) == dt.timedelta(0)
