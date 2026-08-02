"""Integration tests for the Django-free cold posture resolver (#3826).

Every test builds a REAL sqlite control DB with the mode / override / schedule
schema via stdlib `sqlite3` (exactly what Django's migrations produce), then reads it
back through `cold_mode` — no mocks, so the precedence chain and the fail-toward-
asking behaviour are exercised against actual sqlite.

The module this replaces read a JSON mirror file instead of the DB, which is how a
week-old `autonomous_away` outlived the override row it mirrored and muted the owner.
"""

# test-path: cross-cutting — spans the cold reader, the Django resolver and the shared
# live-presence leaf; no single mirror dir owns all three.

import datetime as dt
import json
import sqlite3
from pathlib import Path

from teatree.config import cold_mode
from teatree.config.cold_mode import resolve_cold_posture
from teatree.config.host_projection import ProjectionPublisher
from teatree.live_presence import PRESENCE_FILENAME

NOW = dt.datetime(2026, 7, 28, 12, 0, tzinfo=dt.UTC)

_SCHEMA = """
CREATE TABLE teatree_config_setting (id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '',
    key TEXT NOT NULL, value TEXT NOT NULL);
CREATE TABLE teatree_loop_preset (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
    defers_questions BOOL NOT NULL DEFAULT 0, pauses_self_pump BOOL NOT NULL DEFAULT 0,
    presence_sensitive BOOL NOT NULL DEFAULT 1);
CREATE TABLE teatree_loop_preset_override (id INTEGER PRIMARY KEY, preset_name TEXT NOT NULL,
    until TEXT, reason TEXT DEFAULT '', set_at TEXT NOT NULL);
CREATE TABLE teatree_loop_schedule (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
    timezone TEXT NOT NULL DEFAULT '');
CREATE TABLE teatree_loop_schedule_slot (id INTEGER PRIMARY KEY, schedule_id INTEGER NOT NULL,
    days TEXT NOT NULL, start_time TEXT NOT NULL, preset_name TEXT NOT NULL);
CREATE TABLE teatree_loop_state (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, status TEXT NOT NULL);
"""

#: The three shipped postures, as the seeded mode rows carry them.
_MODES = [("engaged", 0, 0, 1), ("unattended", 1, 0, 1), ("offline", 1, 1, 0)]


def _db(tmp_path: Path) -> Path:
    """A control DB seeded with the three posture-carrying modes and nothing else."""
    path = tmp_path / "db.sqlite3"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_SCHEMA)
        conn.executemany(
            "INSERT INTO teatree_loop_preset "
            "(name, defers_questions, pauses_self_pump, presence_sensitive) VALUES (?,?,?,?)",
            _MODES,
        )
        conn.commit()
    finally:
        conn.close()
    return path


def _exec(db: Path, statement: str, bindings: tuple[object, ...] = ()) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(statement, bindings)
        conn.commit()
    finally:
        conn.close()


def _setting(db: Path, key: str, value: object) -> None:
    _exec(db, "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', ?, ?)", (key, json.dumps(value)))


def _override(db: Path, name: str, *, until: dt.datetime | None = None) -> None:
    _exec(
        db,
        "INSERT INTO teatree_loop_preset_override (preset_name, until, set_at) VALUES (?, ?, ?)",
        (name, None if until is None else until.replace(tzinfo=None).isoformat(sep=" "), "2026-07-28 00:00:00"),
    )


def _schedule(db: Path, preset_name: str, *, timezone: str = "UTC", start: str = "00:00:00") -> None:
    _exec(db, "INSERT INTO teatree_loop_schedule (id, name, timezone) VALUES (1, 'standard', ?)", (timezone,))
    _exec(
        db,
        "INSERT INTO teatree_loop_schedule_slot (schedule_id, days, start_time, preset_name) VALUES (1, ?, ?, ?)",
        (json.dumps([0, 1, 2, 3, 4, 5, 6]), start, preset_name),
    )
    _setting(db, "active_loop_schedule", "standard")


def _keystroke(tmp_path: Path, at: dt.datetime) -> None:
    (tmp_path / PRESENCE_FILENAME).write_text(json.dumps({"at": at.isoformat(), "session": "s"}), encoding="utf-8")


def _posture(source: str, *, defers: bool = False, pauses: bool = False) -> cold_mode.ColdPosture:
    return cold_mode.ColdPosture(defers_questions=defers, pauses_self_pump=pauses, source=source)


def _resolve(tmp_path: Path, db: Path, now: dt.datetime = NOW) -> cold_mode.ColdPosture:
    return resolve_cold_posture(now, db_path=db, data_dir=tmp_path)


def _host_env(tmp_path: Path) -> dict[str, str]:
    """A host's view of the world: the control DB inside a volume it cannot open."""
    return {
        "T3_CONFIG_DB": str(tmp_path / "control-db-volume" / "db.sqlite3"),
        "XDG_DATA_HOME": str(tmp_path / "share"),
    }


def _projected(tmp_path: Path, db: Path, now: dt.datetime = NOW) -> cold_mode.ColdPosture:
    """The posture a HOST resolves for *db*: unopenable there, reached via its projection.

    Publishes *db*'s projection into the data dir the host env resolves to, then resolves
    with no ``db_path`` at all — exactly what a bare hook does.
    """
    data_dir = tmp_path / "share" / "teatree"
    data_dir.mkdir(parents=True, exist_ok=True)
    ProjectionPublisher(db, data_dir).publish()
    return resolve_cold_posture(now, data_dir=tmp_path, env=_host_env(tmp_path))


def _unmigrated_db(tmp_path: Path) -> Path:
    """A control DB from before the mode tables existed — settings and loop state only."""
    path = tmp_path / "unmigrated.sqlite3"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            "CREATE TABLE teatree_config_setting (id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', "
            "key TEXT NOT NULL, value TEXT NOT NULL);"
            "CREATE TABLE teatree_loop_state (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, status TEXT NOT NULL);"
        )
        conn.commit()
    finally:
        conn.close()
    return path


class TestPrecedenceChain:
    def test_no_override_no_schedule_no_default_is_reachable(self, tmp_path: Path) -> None:
        assert _resolve(tmp_path, _db(tmp_path)) == _posture("default", defers=False, pauses=False)

    def test_configured_default_mode_decides(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _setting(db, "default_mode", "unattended")
        assert _resolve(tmp_path, db) == _posture("default", defers=True, pauses=False)

    def test_live_override_beats_the_schedule(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _schedule(db, "engaged")
        _override(db, "offline")
        assert _resolve(tmp_path, db) == _posture("override", defers=True, pauses=True)

    def test_expired_override_falls_through_to_the_schedule(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _schedule(db, "engaged")
        _override(db, "offline", until=NOW - dt.timedelta(hours=1))
        assert _resolve(tmp_path, db) == _posture("schedule", defers=False, pauses=False)

    def test_unexpired_override_still_governs(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _override(db, "unattended", until=NOW + dt.timedelta(hours=1))
        assert _resolve(tmp_path, db) == _posture("override", defers=True, pauses=False)

    def test_schedule_slot_decides_when_no_override(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _schedule(db, "unattended")
        assert _resolve(tmp_path, db) == _posture("schedule", defers=True, pauses=False)

    def test_only_the_newest_override_row_counts(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _override(db, "offline")
        _exec(
            db,
            "INSERT INTO teatree_loop_preset_override (preset_name, until, set_at) VALUES "
            "('engaged', NULL, '2026-07-28 06:00:00')",
        )
        assert _resolve(tmp_path, db).defers_questions is False

    def test_slot_in_a_non_utc_zone_resolves_at_its_local_start(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        # 20:00 Europe/Vienna == 18:00 UTC, so at 12:00 UTC the governing start is
        # YESTERDAY's — the week-wrap lookback, not "no slot yet today".
        _schedule(db, "unattended", timezone="Europe/Vienna", start="20:00:00")
        assert _resolve(tmp_path, db) == _posture("schedule", defers=True, pauses=False)


class TestPresenceUpgrade:
    def test_fresh_keystroke_upgrades_a_scheduled_away_mode(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _schedule(db, "unattended")
        _keystroke(tmp_path, NOW - dt.timedelta(minutes=1))
        assert _resolve(tmp_path, db) == _posture("live", defers=False, pauses=False)

    def test_stale_keystroke_does_not_upgrade(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _schedule(db, "unattended")
        _keystroke(tmp_path, NOW - dt.timedelta(hours=2))
        assert _resolve(tmp_path, db).defers_questions is True

    def test_manual_override_is_never_upgraded_by_a_keystroke(self, tmp_path: Path) -> None:
        # The deliberate rule of `mode_resolution._apply_presence_upgrade`: a human who
        # explicitly said "defer my questions" is not overruled by typing.
        db = _db(tmp_path)
        _override(db, "unattended")
        _keystroke(tmp_path, NOW)
        assert _resolve(tmp_path, db) == _posture("override", defers=True, pauses=False)

    def test_presence_insensitive_mode_is_never_upgraded(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _schedule(db, "offline")
        _keystroke(tmp_path, NOW)
        assert _resolve(tmp_path, db) == _posture("schedule", defers=True, pauses=True)

    def test_upgrade_target_is_repointable(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _schedule(db, "unattended")
        _setting(db, "presence_upgrade_mode", "offline")
        _keystroke(tmp_path, NOW)
        assert _resolve(tmp_path, db) == _posture("live", defers=True, pauses=True)

    def test_corrupt_heartbeat_does_not_upgrade(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _schedule(db, "unattended")
        (tmp_path / PRESENCE_FILENAME).write_text("{not json", encoding="utf-8")
        assert _resolve(tmp_path, db).defers_questions is True


class TestTheHostResolvesTheSamePostureFromTheProjection:
    """The host case: the control DB lives in a volume no host process can open.

    Every case resolves the SAME control-plane state twice — once off the database, once
    off its published projection — and asserts one identical posture. Without the mode
    tables in the projection the projected side answered ``unresolved`` for all of them,
    so a deferring mode was inert on the host: ``AskUserQuestion`` rendered in-client and
    the self-pump kept pumping straight through a holiday.
    """

    def test_an_active_manual_override(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _override(db, "offline")

        assert _projected(tmp_path, db) == _resolve(tmp_path, db) == _posture("override", defers=True, pauses=True)

    def test_an_unexpired_override_window(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _override(db, "unattended", until=NOW + dt.timedelta(hours=1))

        assert _projected(tmp_path, db) == _resolve(tmp_path, db) == _posture("override", defers=True, pauses=False)

    def test_an_expired_override_falls_through_to_the_schedule(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _schedule(db, "engaged")
        _override(db, "offline", until=NOW - dt.timedelta(hours=1))

        assert _projected(tmp_path, db) == _resolve(tmp_path, db) == _posture("schedule", defers=False, pauses=False)

    def test_an_in_slot_schedule(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _schedule(db, "unattended")

        assert _projected(tmp_path, db) == _resolve(tmp_path, db) == _posture("schedule", defers=True, pauses=False)

    def test_a_slot_in_a_non_utc_zone(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _schedule(db, "unattended", timezone="Europe/Vienna", start="20:00:00")

        assert _projected(tmp_path, db) == _resolve(tmp_path, db) == _posture("schedule", defers=True, pauses=False)

    def test_an_out_of_slot_schedule_falls_back_to_the_configured_default(self, tmp_path: Path) -> None:
        # A slot covering no weekday governs at no instant, so the L2 layer yields nothing
        # and the L0 default decides — over the schedule/slot join, on both sides.
        db = _db(tmp_path)
        _setting(db, "default_mode", "unattended")
        _exec(db, "INSERT INTO teatree_loop_schedule (id, name, timezone) VALUES (1, 'standard', 'UTC')")
        _exec(
            db,
            "INSERT INTO teatree_loop_schedule_slot (schedule_id, days, start_time, preset_name) "
            "VALUES (1, '[]', '00:00:00', 'engaged')",
        )
        _setting(db, "active_loop_schedule", "standard")

        assert _projected(tmp_path, db) == _resolve(tmp_path, db) == _posture("default", defers=True, pauses=False)

    def test_the_presence_upgrade(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _schedule(db, "unattended")
        _keystroke(tmp_path, NOW - dt.timedelta(minutes=1))

        assert _projected(tmp_path, db) == _resolve(tmp_path, db) == _posture("live", defers=False, pauses=False)

    def test_the_presence_upgrade_target_is_repointable(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _schedule(db, "unattended")
        _setting(db, "presence_upgrade_mode", "offline")
        _keystroke(tmp_path, NOW)

        assert _projected(tmp_path, db) == _resolve(tmp_path, db) == _posture("live", defers=True, pauses=True)

    def test_a_manual_override_is_still_never_upgraded_by_a_keystroke(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _override(db, "unattended")
        _keystroke(tmp_path, NOW)

        assert _projected(tmp_path, db) == _resolve(tmp_path, db) == _posture("override", defers=True, pauses=False)

    def test_a_projection_carrying_no_mode_rows_resolves_reachable(self, tmp_path: Path) -> None:
        # A publisher from before the mode tables were projected still serves its settings,
        # so `default_mode` is read — but with no mode row to read the posture off, the
        # resolver falls toward asking rather than inventing the deferral it cannot verify.
        db = _unmigrated_db(tmp_path)
        _setting(db, "default_mode", "offline")

        assert _projected(tmp_path, db) == _posture("default", defers=False, pauses=False)


class TestAnExplicitDbPathNeverFallsThroughToTheProjection:
    def test_a_named_absent_database_stays_unresolved_with_a_projection_published(self, tmp_path: Path) -> None:
        # A caller that names a file means that file: answering from a projection of a
        # DIFFERENT database would answer a different question. Both the resolver-parity
        # harness and the reachability guard depend on it.
        db = _db(tmp_path)
        _override(db, "offline")
        data_dir = tmp_path / "share" / "teatree"
        data_dir.mkdir(parents=True)
        ProjectionPublisher(db, data_dir).publish()

        posture = resolve_cold_posture(
            NOW, db_path=tmp_path / "absent.sqlite3", data_dir=tmp_path, env=_host_env(tmp_path)
        )

        assert posture == cold_mode.UNRESOLVED


class TestFailsTowardAsking:
    """An input the resolver cannot read must interrupt the user, never mute them."""

    def test_missing_db_is_reachable(self, tmp_path: Path) -> None:
        assert _resolve(tmp_path, tmp_path / "nope.sqlite3") == cold_mode.UNRESOLVED

    def test_db_without_the_tables_is_reachable(self, tmp_path: Path) -> None:
        db = tmp_path / "fresh.sqlite3"
        sqlite3.connect(db).close()
        assert _resolve(tmp_path, db).defers_questions is False

    def test_override_naming_a_deleted_mode_falls_open_to_the_default(self, tmp_path: Path) -> None:
        # Never invents a posture for a dangling name, and never silently promotes the
        # schedule underneath it — the same fail-open the Django resolver performs.
        db = _db(tmp_path)
        _schedule(db, "offline")
        _override(db, "deleted-mode")
        assert _resolve(tmp_path, db) == _posture("default", defers=False, pauses=False)

    def test_active_schedule_naming_an_unknown_calendar_is_reachable(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _setting(db, "active_loop_schedule", "no-such-calendar")
        assert _resolve(tmp_path, db) == _posture("default", defers=False, pauses=False)

    def test_malformed_slot_days_is_ignored(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _exec(db, "INSERT INTO teatree_loop_schedule (id, name, timezone) VALUES (1, 'standard', 'UTC')")
        _exec(
            db,
            "INSERT INTO teatree_loop_schedule_slot (schedule_id, days, start_time, preset_name) "
            "VALUES (1, 'not-json', '00:00:00', 'offline')",
        )
        _setting(db, "active_loop_schedule", "standard")
        assert _resolve(tmp_path, db).defers_questions is False

    def test_unreadable_override_timestamp_is_treated_as_no_expiry(self, tmp_path: Path) -> None:
        # A garbled `until` must not void the override into silence NOR extend it
        # wrongly into deferral: an unparsable expiry reads as "held", which is what
        # the operator asked for when they set it.
        db = _db(tmp_path)
        _exec(
            db,
            "INSERT INTO teatree_loop_preset_override (preset_name, until, set_at) VALUES "
            "('unattended', 'garbled', '2026-07-28 00:00:00')",
        )
        assert _resolve(tmp_path, db) == _posture("override", defers=True, pauses=False)
