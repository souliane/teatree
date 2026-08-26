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
        self.refuse_all = False
        self.writes: list[str] = []

    def read(self, key: str) -> str:
        return self.values.get(key, "")

    def write(self, key: str, value: str) -> bool:
        self.writes.append(key)
        if self.refuse_all or key in self.refuse:
            return False
        self.values[key] = value
        return True

    def remove(self, key: str) -> bool:
        return self.values.pop(key, None) is not None


def _seeded(*, issued_at: dt.datetime | None, refuse: set[str] | None = None) -> _FakePassStore:
    values = {_STORE.access_key: "xoxe.xoxp-OLD", _STORE.refresh_key: "xoxe-OLDR"}
    if issued_at is not None:
        values[_STORE.issued_at_key] = issued_at.isoformat()
    return _FakePassStore(values, refuse=refuse)


def _run(store: _FakePassStore, rotate: object, *, now: dt.datetime = _NOW):
    with (
        patch("teatree.cli.slack.config_token.read_pass", side_effect=store.read),
        patch("teatree.cli.slack.config_token.write_pass", side_effect=store.write),
        patch("teatree.cli.slack.config_token.remove_pass", side_effect=store.remove),
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

        Pinned as a ratio so the margin survives either constant being retuned:
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

    def test_a_write_that_cannot_be_read_back_is_a_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A `pass insert` reporting success while storing nothing must not pass for persistence.

        Asserted directly against :meth:`ConfigTokenStore.persist`, because the
        write-ahead guard now refuses this store shape before a rotation is ever
        spent (see ``TestRefusesToRotateWhenTheStoreCannotPersist``). This keeps
        the persist-level check pinned as defence in depth, for a store that
        degrades in the window between the probe and the write.
        """
        store = _seeded(issued_at=None)
        monkeypatch.setattr(store, "write", lambda key, value: True)

        with (
            patch("teatree.cli.slack.config_token.read_pass", side_effect=store.read),
            patch("teatree.cli.slack.config_token.write_pass", side_effect=store.write),
            pytest.raises(SlackConfigTokenPersistError),
        ):
            _STORE.persist(access="xoxe.xoxp-NEW", refresh="xoxe-NEWR", issued_at=_NOW)

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


class _WriteSwallowingPassStore(_FakePassStore):
    """A store whose writes REPORT success but never land — the venue failure seen in the container.

    ``pass insert`` only needs the public key, so it exits 0 where ``pass show``
    cannot start ``gpg-agent``/``keyboxd`` and fails. A writability check that
    trusts the write's exit code therefore passes on a store nothing can be read
    back out of, which is exactly the state that turns a rotate into a
    permanently-lost credential.
    """

    def write(self, key: str, value: str) -> bool:
        self.writes.append(key)
        return True


class TestRefusesToRotateWhenTheStoreCannotPersist:
    """Write-ahead: prove the store round-trips BEFORE spending an irreversible rotate.

    Slack invalidates the previous pair the instant ``tooling.tokens.rotate``
    issues a new one, so a rotate whose result cannot be stored destroys the
    credential with no way back. Ordering is the guarantee: the store must be
    proven writable with a real write-then-read of a throwaway value FIRST, and
    Slack must not be called at all when that proof fails.
    """

    def test_rotate_is_never_called_when_the_store_refuses_writes(self) -> None:
        store = _seeded(issued_at=_NOW - ROTATE_AFTER - dt.timedelta(minutes=1))
        store.refuse_all = True
        attempted: list[str] = []

        def record(*, refresh_token: str) -> tuple[str, str]:
            attempted.append(refresh_token)
            return "xoxe.xoxp-NEW", "xoxe-NEWR"

        report = _run(store, record)

        assert attempted == [], "an unwritable store must not spend an irreversible rotate"
        assert report.outcome is RotationOutcome.STORE_UNWRITABLE
        assert store.values[_STORE.refresh_key] == "xoxe-OLDR"

    def test_rotate_is_never_called_when_writes_report_success_but_do_not_land(self) -> None:
        store = _WriteSwallowingPassStore(
            {
                _STORE.access_key: "xoxe.xoxp-OLD",
                _STORE.refresh_key: "xoxe-OLDR",
                _STORE.issued_at_key: (_NOW - ROTATE_AFTER - dt.timedelta(minutes=1)).isoformat(),
            }
        )
        attempted: list[str] = []

        def record(*, refresh_token: str) -> tuple[str, str]:
            attempted.append(refresh_token)
            return "xoxe.xoxp-NEW", "xoxe-NEWR"

        report = _run(store, record)

        assert attempted == [], "a write that cannot be read back must not spend an irreversible rotate"
        assert report.outcome is RotationOutcome.STORE_UNWRITABLE

    def test_the_writability_probe_leaves_no_residue_in_the_store(self) -> None:
        """A healthy rotate still ends with only the three real slots — the probe cleans up after itself."""
        store = _seeded(issued_at=_NOW - ROTATE_AFTER - dt.timedelta(minutes=1))

        report = _run(store, _issues_new_pair)

        assert report.outcome is RotationOutcome.ROTATED
        assert set(store.values) == {_STORE.access_key, _STORE.refresh_key, _STORE.issued_at_key}
