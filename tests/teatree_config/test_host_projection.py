"""The host projection's five properties, each asserted against a planted violation.

The projection is the only thing standing between a container-owned control DB and
fourteen kill-switches silently reverting to their compiled-in defaults (#3499). A
guard for that failure is worth exactly as much as the control that proves it fires,
so every test here plants the bad state and asserts the loud outcome — most of all
:class:`TestStaleProjectionRaisesAnAdvisory`, which is the whole reason the generation
counter exists.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from teatree.config import cold_db, cold_reader, host_projection
from teatree.config.host_projection import (
    GENERATION_KEY,
    GLOBAL_SCOPE,
    HostProjection,
    ModeRow,
    OverrideRow,
    ProjectionPublisher,
    ProjectionReader,
    ScheduleRow,
    SlotRow,
    Staleness,
    next_generation,
)

_SETTINGS_SCHEMA = """
CREATE TABLE teatree_config_setting (
    id INTEGER PRIMARY KEY,
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (scope, key)
);
CREATE TABLE teatree_loop_state (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL
);
"""

#: The four mode tables in the column shapes Django's migrations produce — the ``days``
#: JSONField and the ``start_time`` TimeField are TEXT, which is what the projection
#: hands the cold parsers verbatim.
_MODE_SCHEMA = """
CREATE TABLE teatree_loop_preset (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
    defers_questions BOOL NOT NULL, pauses_self_pump BOOL NOT NULL, presence_sensitive BOOL NOT NULL
);
CREATE TABLE teatree_loop_preset_override (
    id INTEGER PRIMARY KEY, preset_name TEXT NOT NULL, until TEXT, reason TEXT NOT NULL DEFAULT '',
    set_at TEXT NOT NULL
);
CREATE TABLE teatree_loop_schedule (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, timezone TEXT NOT NULL);
CREATE TABLE teatree_loop_schedule_slot (
    id INTEGER PRIMARY KEY, schedule_id INTEGER NOT NULL, days TEXT NOT NULL,
    start_time TEXT NOT NULL, preset_name TEXT NOT NULL
);
"""

#: The four mode tables, in the column shapes Django's migrations produce — the ``days``
#: JSONField and the ``start_time`` TimeField are TEXT, which is what the projection
#: hands the cold parsers verbatim.
_MODE_SCHEMA = """
CREATE TABLE teatree_loop_preset (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
    defers_questions BOOL NOT NULL, pauses_self_pump BOOL NOT NULL, presence_sensitive BOOL NOT NULL
);
CREATE TABLE teatree_loop_preset_override (
    id INTEGER PRIMARY KEY, preset_name TEXT NOT NULL, until TEXT, reason TEXT NOT NULL DEFAULT '',
    set_at TEXT NOT NULL
);
CREATE TABLE teatree_loop_schedule (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, timezone TEXT NOT NULL);
CREATE TABLE teatree_loop_schedule_slot (
    id INTEGER PRIMARY KEY, schedule_id INTEGER NOT NULL, days TEXT NOT NULL,
    start_time TEXT NOT NULL, preset_name TEXT NOT NULL
);
"""


def _build_source(db_path: Path, settings: dict[tuple[str, str], object], loops: dict[str, str]) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_SETTINGS_SCHEMA)
        conn.executemany(
            "INSERT INTO teatree_config_setting (scope, key, value, created_at, updated_at) VALUES (?, ?, ?, '', '')",
            [(scope, key, json.dumps(value)) for (scope, key), value in settings.items()],
        )
        conn.executemany("INSERT INTO teatree_loop_state (name, status) VALUES (?, ?)", list(loops.items()))
        conn.commit()
    finally:
        conn.close()


def _seed_mode_tables(db_path: Path) -> None:
    """Two modes, two override rows (only the newest governs), one two-slot schedule."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_MODE_SCHEMA)
        conn.executemany(
            "INSERT INTO teatree_loop_preset (name, defers_questions, pauses_self_pump, presence_sensitive) "
            "VALUES (?, ?, ?, ?)",
            [("engaged", 0, 0, 1), ("unattended", 1, 0, 1)],
        )
        conn.executemany(
            "INSERT INTO teatree_loop_preset_override (preset_name, until, set_at) VALUES (?, ?, ?)",
            [("offline", "2026-07-01 08:00:00", "2026-07-01 07:00:00"), ("unattended", None, "2026-07-28 07:00:00")],
        )
        conn.execute("INSERT INTO teatree_loop_schedule (id, name, timezone) VALUES (1, 'standard', 'Europe/Vienna')")
        conn.executemany(
            "INSERT INTO teatree_loop_schedule_slot (schedule_id, days, start_time, preset_name) VALUES (?, ?, ?, ?)",
            [(1, "[0, 1, 2, 3, 4]", "09:00:00", "engaged"), (1, "[5, 6]", "20:00:00", "unattended")],
        )
        conn.commit()
    finally:
        conn.close()


def _mode_projection() -> HostProjection:
    """A current-schema projection carrying one row of each mode table."""
    return HostProjection(
        generation=3,
        settings={GLOBAL_SCOPE: {"autoload": True}},
        loop_state={"dispatch": "enabled"},
        modes={"unattended": ModeRow(defers_questions=True, pauses_self_pump=False, presence_sensitive=True)},
        mode_override=OverrideRow(preset_name="unattended", until=""),
        mode_schedules={"standard": ScheduleRow(schedule_id="1", timezone="UTC")},
        mode_schedule_slots={
            "1": (SlotRow(days="[0, 1, 2, 3, 4, 5, 6]", start_time="09:00:00", preset_name="engaged"),)
        },
        source="/var/lib/teatree/control-db/db.sqlite3",
        projected_at="2026-07-28T00:00:00+00:00",
    )


def _previous_schema_payload() -> dict[str, object]:
    """Exactly what a publisher from before the mode tables were projected emits."""
    return {
        "schema_version": 1,
        "generation": 22,
        "source": "/var/lib/teatree/control-db/db.sqlite3",
        "projected_at": "2026-07-30T06:00:04+00:00",
        "settings": {GLOBAL_SCOPE: {"memory_recall_enabled": False}},
        "loop_state": {"dispatch": "paused"},
    }


@pytest.fixture
def source_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "volume" / "db.sqlite3"
    db_path.parent.mkdir()
    _build_source(
        db_path,
        {
            (GLOBAL_SCOPE, "memory_recall_enabled"): False,
            (GLOBAL_SCOPE, GENERATION_KEY): 7,
            ("demo-overlay", "mode"): "auto",
        },
        {"dispatch": "paused"},
    )
    _seed_mode_tables(db_path)
    return db_path


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    published = tmp_path / "data"
    published.mkdir()
    return published


class TestProjectionCarriesWhatTheHooksRead:
    def test_publishes_every_scope_and_every_loop_status(self, source_db: Path, data_dir: Path) -> None:
        published = ProjectionPublisher(source_db, data_dir).publish()

        assert published.setting("memory_recall_enabled") is False
        assert published.setting("mode", scope="demo-overlay") == "auto"
        assert published.loop_status("dispatch") == "paused"

    def test_generation_comes_from_the_source_not_the_file(self, source_db: Path, data_dir: Path) -> None:
        assert ProjectionPublisher(source_db, data_dir).publish().generation == 7

    def test_a_reader_sees_exactly_what_was_published(self, source_db: Path, data_dir: Path) -> None:
        published = ProjectionPublisher(source_db, data_dir).publish()

        read = ProjectionReader(data_dir).read()

        assert read.staleness is Staleness.FRESH
        assert read.projection == published


class TestAtomicPublication:
    def test_replaces_by_rename_so_a_reader_never_sees_a_torn_file(self, source_db: Path, data_dir: Path) -> None:
        publisher = ProjectionPublisher(source_db, data_dir)
        publisher.publish()
        first_inode = publisher.target.stat().st_ino

        publisher.publish()

        assert publisher.target.stat().st_ino != first_inode, (
            "an in-place rewrite reuses the inode; a reader mid-read would see a torn file"
        )

    def test_leaves_no_temp_file_behind(self, source_db: Path, data_dir: Path) -> None:
        ProjectionPublisher(source_db, data_dir).publish()

        assert [entry.name for entry in data_dir.iterdir() if entry.name.endswith(".tmp")] == []


class TestGenerationRatchet:
    @pytest.mark.parametrize(
        ("stored", "expected"),
        [(None, 1), (0, 1), (7, 8), (True, 1), ("nine", 1), (-3, 1)],
    )
    def test_only_ever_moves_forward(self, stored: object, expected: int) -> None:
        assert next_generation(stored) == expected


class TestStaleProjectionRaisesAnAdvisory:
    """The control the whole design rests on: a stale projection must be LOUD.

    Without this, a projection that stops being republished is indistinguishable from
    a fresh one and the hooks quietly serve old kill-switch values — which is exactly
    how #3499 stayed invisible for months.
    """

    def test_a_planted_stale_generation_is_refused_with_an_advisory(self, source_db: Path, data_dir: Path) -> None:
        reader = ProjectionReader(data_dir)
        ProjectionPublisher(source_db, data_dir).publish()
        assert reader.read().staleness is Staleness.FRESH, "control: the fresh projection must read clean"

        _plant_generation(reader.target, 3)
        stale = reader.read()

        assert stale.staleness is Staleness.REGRESSED
        assert stale.projection is not None, "the values are still parsed — the advisory is about trusting them"
        assert not stale.trustworthy
        assert "generation 3" in stale.advisory
        assert "compiled-in" in stale.advisory

    def test_a_stale_projection_does_not_answer_a_cold_setting_read(
        self, source_db: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reader = ProjectionReader(data_dir)
        ProjectionPublisher(source_db, data_dir).publish()
        monkeypatch.setattr(cold_db, "canonical_data_dir", lambda **_: data_dir)
        monkeypatch.setattr(cold_db, "canonical_config_db", lambda **_: data_dir / "absent.sqlite3")
        assert cold_reader.bool_setting("memory_recall_enabled", default=True) is False, (
            "control: the fresh projection must be what the cold reader answers from"
        )

        _plant_generation(reader.target, 3)

        assert cold_reader.bool_setting("memory_recall_enabled", default=True) is True, (
            "a stale projection must fall back to the compiled-in default, never serve its own stored value"
        )

    def test_an_absent_projection_is_an_advisory_not_a_silent_default(self, data_dir: Path) -> None:
        read = ProjectionReader(data_dir).read()

        assert read.staleness is Staleness.ABSENT
        assert read.projection is None
        assert "no host projection has been published" in read.advisory

    def test_a_malformed_projection_is_an_advisory(self, data_dir: Path) -> None:
        (data_dir / "host-projection.json").write_text("{not json", encoding="utf-8")

        read = ProjectionReader(data_dir).read()

        assert read.staleness is Staleness.UNREADABLE
        assert not read.trustworthy

    def test_a_future_schema_is_refused_rather_than_misparsed(self, source_db: Path, data_dir: Path) -> None:
        reader = ProjectionReader(data_dir)
        ProjectionPublisher(source_db, data_dir).publish()
        payload = json.loads(reader.target.read_text(encoding="utf-8"))
        payload["schema_version"] = 99
        reader.target.write_text(json.dumps(payload), encoding="utf-8")

        read = reader.read()

        assert read.staleness is Staleness.SCHEMA_MISMATCH
        assert "schema v99" in read.advisory


class TestColdReadersFallThroughToTheProjection:
    """The five Django-free hooks reach the DB through these two functions and no other."""

    @pytest.fixture(autouse=True)
    def _unreachable_source(self, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cold_db, "canonical_data_dir", lambda **_: data_dir)
        monkeypatch.setattr(cold_db, "canonical_config_db", lambda **_: Path("/var/lib/teatree/control-db/db.sqlite3"))

    def test_settings_resolve_from_the_projection(self, source_db: Path, data_dir: Path) -> None:
        ProjectionPublisher(source_db, data_dir).publish()

        assert cold_reader.read_setting("memory_recall_enabled") is False
        assert cold_reader.read_setting("mode", scope="demo-overlay") == "auto"

    def test_loop_status_resolves_from_the_projection(self, source_db: Path, data_dir: Path) -> None:
        ProjectionPublisher(source_db, data_dir).publish()

        assert cold_db.loop_status("dispatch") == "paused"

    def test_an_unpublished_projection_leaves_the_compiled_in_default(self) -> None:
        assert cold_db.loop_status("dispatch") == "enabled"
        assert cold_reader.read_setting("memory_recall_enabled") is None


class TestProjectionHasZeroAuthority:
    def test_the_reader_offers_no_way_to_write_the_source(self) -> None:
        assert not [name for name in dir(ProjectionReader) if name.startswith(("write", "set", "publish"))]

    def test_a_projection_hand_edited_to_a_higher_generation_never_reaches_the_source(
        self, source_db: Path, data_dir: Path
    ) -> None:
        ProjectionPublisher(source_db, data_dir).publish()
        _plant_generation(data_dir / "host-projection.json", 4242)

        conn = sqlite3.connect(source_db)
        try:
            stored = conn.execute(
                "SELECT value FROM teatree_config_setting WHERE scope=? AND key=?",
                (GLOBAL_SCOPE, GENERATION_KEY),
            ).fetchone()
        finally:
            conn.close()

        assert json.loads(stored[0]) == 7, "the source is the authority; the file is strictly derived"


class TestProjectionCarriesTheModeTables:
    """The four tables the posture resolver walks, on the side that cannot open the DB."""

    def test_publishes_every_mode_posture(self, source_db: Path, data_dir: Path) -> None:
        published = ProjectionPublisher(source_db, data_dir).publish()

        assert published.mode("unattended") == ModeRow(
            defers_questions=True, pauses_self_pump=False, presence_sensitive=True
        )
        assert published.mode("engaged") == ModeRow(
            defers_questions=False, pauses_self_pump=False, presence_sensitive=True
        )
        assert published.mode("no-such-mode") is None

    def test_publishes_only_the_override_row_that_governs(self, source_db: Path, data_dir: Path) -> None:
        # The resolver reads `ORDER BY set_at DESC LIMIT 1`; carrying the older row too
        # would offer the host an answer the container never had.
        published = ProjectionPublisher(source_db, data_dir).publish()

        assert published.mode_override == OverrideRow(preset_name="unattended", until="")

    def test_slots_are_reachable_through_their_schedules_join_key(self, source_db: Path, data_dir: Path) -> None:
        published = ProjectionPublisher(source_db, data_dir).publish()
        schedule = published.schedule("standard")

        assert schedule == ScheduleRow(schedule_id="1", timezone="Europe/Vienna")
        assert set(published.slots(schedule.schedule_id)) == {
            SlotRow(days="[0, 1, 2, 3, 4]", start_time="09:00:00", preset_name="engaged"),
            SlotRow(days="[5, 6]", start_time="20:00:00", preset_name="unattended"),
        }
        assert published.slots("no-such-schedule") == ()

    def test_every_column_is_projected_as_its_raw_text(self, source_db: Path, data_dir: Path) -> None:
        # `days`, `start_time` and `until` stay exactly what the column holds, so the one
        # weekday / time / datetime parser in `cold_mode` remains the only interpreter.
        published = ProjectionPublisher(source_db, data_dir).publish()

        assert {slot.days for slot in published.slots("1")} == {"[0, 1, 2, 3, 4]", "[5, 6]"}
        assert published.mode_override.until == "", "a NULL expiry projects as empty, which reads as 'holds'"

    def test_absent_mode_tables_still_publish_the_settings(self, tmp_path: Path, data_dir: Path) -> None:
        # A control plane whose mode tables are not migrated yet must not forfeit the
        # kill-switch settings — that is #3499's failure with a new cause.
        db = tmp_path / "unmigrated" / "db.sqlite3"
        db.parent.mkdir()
        _build_source(db, {(GLOBAL_SCOPE, "memory_recall_enabled"): False}, {"dispatch": "paused"})

        published = ProjectionPublisher(db, data_dir).publish()

        assert published.setting("memory_recall_enabled") is False
        assert published.modes == {}
        assert published.mode_override is None


class TestModePayloadShapeIsTotal:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            (
                "modes",
                {"unattended": {"defers_questions": "yes", "pauses_self_pump": False, "presence_sensitive": True}},
            ),
            ("modes", {"unattended": ["defers", "pauses", "sensitive"]}),
            ("modes", "unattended"),
            ("mode_override", {"preset_name": "unattended"}),
            ("mode_override", {"preset_name": "unattended", "until": 7}),
            ("mode_schedules", {"standard": {"timezone": "UTC"}}),
            ("mode_schedule_slots", {"1": {"days": "[]", "start_time": "09:00:00", "preset_name": "engaged"}}),
            ("mode_schedule_slots", {"1": [{"days": "[]", "start_time": "09:00:00"}]}),
        ],
    )
    def test_a_malformed_mode_field_refuses_the_whole_payload(self, field: str, value: object) -> None:
        payload = _mode_projection().as_payload()
        payload[field] = value

        assert HostProjection.from_payload(payload) is None

    def test_an_absent_override_is_not_a_malformed_one(self) -> None:
        payload = _mode_projection().as_payload()
        payload["mode_override"] = None

        rebuilt = HostProjection.from_payload(payload)

        assert rebuilt is not None
        assert rebuilt.mode_override is None


class TestAnOlderPublishersPayloadIsStillServed:
    """The deploy window: the reader ships with the source tree, the publisher in the container.

    Refusing the older shape outright would put every host cold read back on its
    compiled-in default for as long as that window lasts — the same outage the projection
    exists to prevent, with the version bump as its new cause.
    """

    def test_a_previous_schema_still_answers_settings_and_loop_status(self, data_dir: Path) -> None:
        reader = ProjectionReader(data_dir)
        reader.target.write_text(json.dumps(_previous_schema_payload()), encoding="utf-8")

        read = reader.read()

        assert read.trustworthy
        assert read.projection.setting("memory_recall_enabled") is False
        assert read.projection.loop_status("dispatch") == "paused"

    def test_a_previous_schema_says_the_mode_rows_are_not_projected_yet(self, data_dir: Path) -> None:
        reader = ProjectionReader(data_dir)
        reader.target.write_text(json.dumps(_previous_schema_payload()), encoding="utf-8")

        read = reader.read()

        assert read.projection.modes == {}
        assert read.projection.mode_override is None
        assert "no mode rows" in read.advisory
        assert "not been redeployed" in read.advisory

    def test_a_cold_setting_read_still_resolves_from_a_previous_schema(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (data_dir / "host-projection.json").write_text(json.dumps(_previous_schema_payload()), encoding="utf-8")
        monkeypatch.setattr(cold_db, "canonical_data_dir", lambda **_: data_dir)
        monkeypatch.setattr(cold_db, "canonical_config_db", lambda **_: data_dir / "absent.sqlite3")

        assert cold_reader.bool_setting("memory_recall_enabled", default=True) is False

    @pytest.mark.parametrize("version", [0, 99, "two", None])
    def test_a_version_outside_the_readable_range_is_refused(self, data_dir: Path, version: object) -> None:
        reader = ProjectionReader(data_dir)
        reader.target.write_text(
            json.dumps({**_previous_schema_payload(), "schema_version": version}), encoding="utf-8"
        )

        read = reader.read()

        assert read.staleness is Staleness.SCHEMA_MISMATCH
        assert not read.trustworthy


def _plant_generation(target: Path, generation: int) -> None:
    """Rewrite the published projection to an older generation, leaving it otherwise sound."""
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["generation"] = generation
    target.write_text(json.dumps(payload), encoding="utf-8")


def test_projection_round_trips_through_its_payload() -> None:
    projection = _mode_projection()

    assert HostProjection.from_payload(projection.as_payload()) == projection


class TestAdvisorySilenceSeam:
    """The silence seam must gag the advisory ONLY when set — both directions asserted.

    The advisory is the entire remedy for #3499's silence, so a seam that suppressed it
    unconditionally would reintroduce that bug wholesale while every test still passed.
    """

    def test_advisory_reaches_stderr_when_the_seam_is_unset(self, capsys: pytest.CaptureFixture[str]) -> None:
        host_projection._warned.clear()

        host_projection.warn_once("projection is absent", env={})

        assert "projection is absent" in capsys.readouterr().err

    def test_advisory_is_silenced_when_the_seam_is_set(self, capsys: pytest.CaptureFixture[str]) -> None:
        host_projection._warned.clear()

        host_projection.warn_once("projection is absent", env={host_projection.SILENCE_ADVISORY_ENV: "1"})

        assert capsys.readouterr().err == ""

    def test_a_repeated_advisory_is_written_once(self, capsys: pytest.CaptureFixture[str]) -> None:
        host_projection._warned.clear()

        host_projection.warn_once("projection is absent", env={})
        host_projection.warn_once("projection is absent", env={})

        assert capsys.readouterr().err.count("projection is absent") == 1
