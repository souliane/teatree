"""Tests for :mod:`teatree.core.availability` — the surviving presence + mirror primitives (#58, #61).

Post-merge the standalone availability resolver is gone (the mode is resolved by
:mod:`teatree.core.mode_resolution`). What remains here is the live-presence
heartbeat (:class:`PresenceHeartbeat`, the resolver's presence-sensitivity input +
the #189 per-turn escape), the durable file paths, and the pending-question queue.
"""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

from teatree.core import availability
from teatree.core.availability import LIVE_TURN_FRESHNESS, PresenceHeartbeat
from teatree.paths import DATA_DIR


@pytest.fixture
def presence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PresenceHeartbeat:
    target = tmp_path / "availability_presence"
    heartbeat = PresenceHeartbeat(locate=lambda: target)
    monkeypatch.setattr(availability, "PRESENCE", heartbeat)
    return heartbeat


class TestPresenceHeartbeat:
    def test_record_then_load_round_trips(self, presence: PresenceHeartbeat) -> None:
        presence.record()
        loaded = presence.last_seen()
        assert loaded is not None
        assert datetime.now(tz=UTC) - loaded < timedelta(seconds=5)

    def test_load_returns_none_when_absent(self, presence: PresenceHeartbeat) -> None:
        assert presence.last_seen() is None

    def test_load_returns_none_on_corrupt_file(self, presence: PresenceHeartbeat) -> None:
        target = presence.locate()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("not a timestamp", encoding="utf-8")
        assert presence.last_seen() is None

    def test_load_returns_none_on_empty_file(self, presence: PresenceHeartbeat) -> None:
        target = presence.locate()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("   \n", encoding="utf-8")
        assert presence.last_seen() is None

    def test_record_writes_atomically(self, presence: PresenceHeartbeat) -> None:
        target = presence.record()
        leftovers = [p for p in target.parent.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []

    def test_record_accepts_explicit_now(self, presence: PresenceHeartbeat) -> None:
        moment = datetime(2026, 6, 2, 22, 0, tzinfo=UTC)
        presence.record(now=moment)
        assert presence.last_seen() == moment

    def test_record_naive_now_is_assumed_utc(self, presence: PresenceHeartbeat) -> None:
        naive = datetime(2026, 6, 2, 22, 0)  # noqa: DTZ001 — deliberately naive for the guard test.
        presence.record(now=naive)
        loaded = presence.last_seen()
        assert loaded == naive.replace(tzinfo=UTC)


class TestIsLiveUserTurnKillProof:
    """Mutation kill-proof for ``PresenceHeartbeat.is_live_user_turn`` (#2058).

    ``availability.py`` is a high-value mutation module whose diff-scoped
    mutmut run executes ONLY this file. Each assertion pins one mutable point
    so a mutant (guard-negation flip, comparison-operator swap, return-value
    flip, ``or``→``and``) is caught here rather than surviving.
    """

    AT = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)

    def test_empty_session_id_returns_false(self, presence: PresenceHeartbeat) -> None:
        # Kills the ``if not session_id`` negation flip: a stamped same-session
        # turn exists, so only the empty-id guard can produce False here.
        presence.record(session_id="s-a", now=self.AT)
        assert presence.is_live_user_turn(session_id="", now=self.AT + timedelta(seconds=1)) is False

    def test_fresh_same_session_returns_true(self, presence: PresenceHeartbeat) -> None:
        # Kills the ``return False`` → ``return True`` flips on the guard arms
        # and the final ``<=`` → ``<``/``>``/``>=`` swaps within the window.
        presence.record(session_id="s-a", now=self.AT)
        assert presence.is_live_user_turn(session_id="s-a", now=self.AT + timedelta(seconds=1)) is True

    def test_no_recorded_turn_returns_false(self, presence: PresenceHeartbeat) -> None:
        # Kills the ``turn is None`` arm: nothing stamped.
        assert presence.is_live_user_turn(session_id="s-a", now=self.AT) is False

    def test_foreign_session_returns_false(self, presence: PresenceHeartbeat) -> None:
        # Kills the ``turn.session_id != session_id`` comparison flip and the
        # ``or`` → ``and`` swap (a fresh turn exists, only the session differs).
        presence.record(session_id="s-a", now=self.AT)
        assert presence.is_live_user_turn(session_id="s-b", now=self.AT + timedelta(seconds=1)) is False

    def test_at_exact_window_boundary_is_live(self, presence: PresenceHeartbeat) -> None:
        # Kills ``<=`` → ``<``: exactly at the boundary must still be live.
        presence.record(session_id="s-a", now=self.AT)
        assert presence.is_live_user_turn(session_id="s-a", now=self.AT + LIVE_TURN_FRESHNESS) is True

    def test_one_microsecond_past_window_is_not_live(self, presence: PresenceHeartbeat) -> None:
        # Kills ``<=`` → ``>=``/``>``: just past the boundary must defer.
        presence.record(session_id="s-a", now=self.AT)
        past = self.AT + LIVE_TURN_FRESHNESS + timedelta(microseconds=1)
        assert presence.is_live_user_turn(session_id="s-a", now=past) is False

    def test_explicit_now_is_honored_over_wall_clock(self, presence: PresenceHeartbeat) -> None:
        # Kills the ``now or datetime.now(tz=UTC)`` default mutation: with an
        # ancient stamp, a same-instant explicit ``now`` is live; the wall clock
        # (years later) would make it stale.
        presence.record(session_id="s-a", now=self.AT)
        assert presence.is_live_user_turn(session_id="s-a", now=self.AT) is True


class TestRefreshLiveTurnKillProof:
    """Mutation kill-proof for ``PresenceHeartbeat.refresh_live_turn`` (#2058).

    The slide must re-stamp ONLY an already-live same-session turn and return
    whether it did. Each assertion pins a mutable point: the guard negation,
    the ``record`` call, the two return-value flips, and the ``now`` default.
    """

    AT = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)

    def test_live_turn_is_restamped_to_now_and_returns_true(self, presence: PresenceHeartbeat) -> None:
        # Kills the dropped ``self.record(...)`` (the stamp must move to ``now``)
        # and the ``return True`` → ``return False`` flip.
        presence.record(session_id="s-a", now=self.AT)
        slid_to = self.AT + timedelta(seconds=30)
        assert presence.refresh_live_turn(session_id="s-a", now=slid_to) is True
        turn = presence.last_user_turn()
        assert turn is not None
        assert turn.at == slid_to
        assert turn.session_id == "s-a"

    def test_not_live_turn_is_a_noop_and_returns_false(self, presence: PresenceHeartbeat) -> None:
        # Kills the ``if not self.is_live_user_turn`` negation flip and the
        # ``return False`` → ``return True`` flip: nothing stamped → no-op.
        assert presence.refresh_live_turn(session_id="s-loop", now=self.AT) is False
        assert presence.last_user_turn() is None

    def test_stale_turn_is_not_revived(self, presence: PresenceHeartbeat) -> None:
        # Kills the guard flip on the stale path: a turn aged past the window
        # must NOT be re-stamped (the original stamp is unchanged).
        presence.record(session_id="s-a", now=self.AT)
        stale = self.AT + LIVE_TURN_FRESHNESS + timedelta(seconds=1)
        assert presence.refresh_live_turn(session_id="s-a", now=stale) is False
        turn = presence.last_user_turn()
        assert turn is not None
        assert turn.at == self.AT

    def test_foreign_session_is_not_restamped(self, presence: PresenceHeartbeat) -> None:
        # Kills the guard's session arm reaching the slide: a foreign session
        # must not move the recorded stamp or change its session.
        presence.record(session_id="s-a", now=self.AT)
        assert presence.refresh_live_turn(session_id="s-b", now=self.AT + timedelta(seconds=5)) is False
        turn = presence.last_user_turn()
        assert turn is not None
        assert turn.session_id == "s-a"
        assert turn.at == self.AT

    def test_explicit_now_drives_the_restamp_value(self, presence: PresenceHeartbeat) -> None:
        # Kills the ``now or datetime.now(tz=UTC)`` default mutation: the stamp
        # lands at the explicit ``now``, not the wall clock.
        presence.record(session_id="s-a", now=self.AT)
        explicit = self.AT + timedelta(seconds=10)
        presence.refresh_live_turn(session_id="s-a", now=explicit)
        turn = presence.last_user_turn()
        assert turn is not None
        assert turn.at == explicit


class TestDurableFilePaths:
    """``override_path`` / ``presence_path`` resolve to their exact DATA_DIR files."""

    def test_override_path_is_data_dir_json(self) -> None:
        # Kills the ``/`` -> ``*`` operator swap (raises TypeError when called)
        # and the filename XX-wrap / upper-case mutations.
        assert availability.override_path() == DATA_DIR / "availability_override.json"

    def test_presence_path_is_data_dir_presence(self) -> None:
        assert availability.presence_path() == DATA_DIR / "availability_presence"


def _seed_schedule(db: Path, value: object) -> None:
    """Seed the DB-home ``availability_schedule`` setting the cold reader resolves."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES (?, ?, ?)",
            ("", "availability_schedule", json.dumps(value)),
        )
        conn.commit()
    finally:
        conn.close()


class TestPresenceRecordDurability:
    """``PresenceHeartbeat.record`` writes a UTC-aware, atomic, utf-8 heartbeat."""

    AT = datetime(2026, 6, 2, 22, 0, tzinfo=UTC)

    def test_naive_now_written_as_utc_aware(self, presence: PresenceHeartbeat) -> None:
        # Kills the ``moment.tzinfo is None`` flip and ``replace(tzinfo=None)``:
        # a naive ``now`` is stamped with a UTC offset on disk. (The read path
        # re-normalises, so this must be observed on the raw file.)
        target = presence.record(now=datetime(2026, 6, 2, 22, 0))  # noqa: DTZ001 — deliberately naive now
        doc = json.loads(target.read_text(encoding="utf-8"))
        assert doc["at"] == "2026-06-02T22:00:00+00:00"

    def test_default_session_id_is_empty(self, presence: PresenceHeartbeat) -> None:
        # Kills the ``session_id: str = ""`` default mutation (-> "XXXX").
        target = presence.record(now=self.AT)
        doc = json.loads(target.read_text(encoding="utf-8"))
        assert doc["session"] == ""

    def test_creates_missing_parent_directories(self, tmp_path: Path) -> None:
        # Kills the record ``mkdir(parents=...)`` mutants.
        target = tmp_path / "deep" / "nested" / "presence"
        PresenceHeartbeat(locate=lambda: target).record(now=self.AT)
        assert target.is_file()

    def test_atomic_write_uses_named_temp_utf8_sorted(
        self, presence: PresenceHeartbeat, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Kills the record mkstemp prefix/suffix/dir, fdopen encoding, and
        # json.dump sort_keys mutants.
        mkstemp = mock.MagicMock(wraps=availability.tempfile.mkstemp)
        fdopen = mock.MagicMock(wraps=availability.os.fdopen)
        dump = mock.MagicMock(wraps=availability.json.dump)
        monkeypatch.setattr(availability.tempfile, "mkstemp", mkstemp)
        monkeypatch.setattr(availability.os, "fdopen", fdopen)
        monkeypatch.setattr(availability.json, "dump", dump)
        target = presence.record(now=self.AT)
        mkstemp.assert_called_once_with(prefix=".presence-", suffix=".tmp", dir=str(target.parent))
        assert fdopen.call_args.kwargs.get("encoding") == "utf-8"
        assert dump.call_args.kwargs.get("sort_keys") is True

    def test_temp_is_unlinked_when_write_fails(
        self, presence: PresenceHeartbeat, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Kills the record cleanup-path ``unlink(missing_ok=True)`` mutants.
        monkeypatch.setattr(availability.json, "dump", mock.MagicMock(side_effect=RuntimeError("boom")))
        real_unlink = Path.unlink
        seen: list[dict[str, object]] = []

        def spy_unlink(self: Path, **kwargs: object) -> None:
            seen.append(kwargs)
            return real_unlink(self, **kwargs)

        monkeypatch.setattr(Path, "unlink", spy_unlink)
        with pytest.raises(RuntimeError):
            presence.record(now=self.AT)
        assert {"missing_ok": True} in seen


class TestLastUserTurnNormalization:
    """A legacy naive plain-ISO heartbeat is read back as a UTC-aware turn."""

    def test_legacy_naive_timestamp_is_made_utc_aware(self, presence: PresenceHeartbeat) -> None:
        # Kills the read-path ``at.tzinfo is None`` flip, the ``at = None`` drop,
        # and ``replace(tzinfo=None)``.
        target = presence.locate()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("2026-06-02T22:00:00", encoding="utf-8")  # legacy naive ISO
        turn = presence.last_user_turn()
        assert turn is not None
        assert turn.at == datetime(2026, 6, 2, 22, 0, tzinfo=UTC)
        assert turn.at.tzinfo is not None

    def test_reads_the_heartbeat_as_utf8(self, presence: PresenceHeartbeat, monkeypatch: pytest.MonkeyPatch) -> None:
        # Kills the ``read_text(encoding="utf-8")`` mutants: a non-ASCII session
        # id round-trips only under an explicit utf-8 read.
        target = presence.locate()
        target.parent.mkdir(parents=True, exist_ok=True)
        real_read = Path.read_text
        seen: list[dict[str, object]] = []

        def spy_read(self: Path, **kwargs: object) -> str:
            seen.append(kwargs)
            return real_read(self, **kwargs)

        target.write_text('{"at": "2026-06-02T22:00:00+00:00", "session": "s-a"}', encoding="utf-8")
        monkeypatch.setattr(Path, "read_text", spy_read)
        presence.last_user_turn()
        assert seen == [{"encoding": "utf-8"}]


class TestLiveTurnWallClockDefault:
    """``is_live_user_turn`` / ``refresh_live_turn`` default ``now`` to a UTC clock."""

    def test_is_live_user_turn_defaults_now_to_utc(self, presence: PresenceHeartbeat) -> None:
        # Kills ``datetime.now(tz=UTC)`` -> ``tz=None``: a naive wall clock would
        # raise on ``naive - aware`` when comparing against the aware stamp.
        presence.record(session_id="s-a", now=datetime.now(tz=UTC))
        assert presence.is_live_user_turn(session_id="s-a") is True

    def test_refresh_live_turn_defaults_now_to_utc(self, presence: PresenceHeartbeat) -> None:
        presence.record(session_id="s-a", now=datetime.now(tz=UTC))
        assert presence.refresh_live_turn(session_id="s-a") is True


class TestPendingQuestions:
    """``pending_questions_count`` / ``iter_pending_questions`` honour ``using``."""

    # ast-grep-ignore: ac-django-no-pytest-django-db
    @pytest.mark.django_db
    def test_count_reflects_pending_rows(self) -> None:
        assert availability.pending_questions_count() == 0
        availability.DeferredQuestion.record("q1")
        availability.DeferredQuestion.record("q2")
        assert availability.pending_questions_count() == 2

    # ast-grep-ignore: ac-django-no-pytest-django-db
    @pytest.mark.django_db
    def test_count_forwards_using(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Kills ``pending(using=using)`` -> ``using=None``: the caller's DB alias
        # must be forwarded, not silently replaced.
        seen: list[str | None] = []
        real_pending = availability.DeferredQuestion.pending.__func__

        def spy(cls: type, *, using: str | None = None) -> object:
            seen.append(using)
            return real_pending(cls, using=using)

        monkeypatch.setattr(availability.DeferredQuestion, "pending", classmethod(spy))
        availability.DeferredQuestion.record("q1")
        assert availability.pending_questions_count(using="default") == 1
        assert seen == ["default"]

    # ast-grep-ignore: ac-django-no-pytest-django-db
    @pytest.mark.django_db
    def test_iter_forwards_using(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[str | None] = []
        real_pending = availability.DeferredQuestion.pending.__func__

        def spy(cls: type, *, using: str | None = None) -> object:
            seen.append(using)
            return real_pending(cls, using=using)

        monkeypatch.setattr(availability.DeferredQuestion, "pending", classmethod(spy))
        availability.DeferredQuestion.record("q1")
        assert len(list(availability.iter_pending_questions(using="default"))) == 1
        assert seen == ["default"]
