"""The host projection's five properties, each asserted against a planted violation.

The projection is the only thing standing between a container-owned control DB and
fourteen kill-switches silently reverting to their compiled-in defaults (#3499). A
guard for that failure is worth exactly as much as the control that proves it fires,
so every test here plants the bad state and asserts the loud outcome — most of all
:class:`TestStaleProjectionRaisesAnAdvisory`, which is the whole reason the generation
counter exists.
"""

import fcntl
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from teatree.config import cold_db, cold_reader, host_projection
from teatree.config.host_projection import (
    GENERATION_KEY,
    GLOBAL_SCOPE,
    HostProjection,
    ProjectionPublisher,
    ProjectionReader,
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
    def _unreachable_source(self, tmp_path: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The unreachable source is pinned inside this test's OWN tmp dir, never at the
        # literal `DEFAULT_CONTROL_DB_DIR / DB_FILENAME`. That literal is a live path — the
        # control-DB volume mount, and what `paths.TRUE_CANONICAL_DB` resolves to — so in a
        # container it is PRESENT, `loop_status` takes its `db.exists()` branch, and the
        # fall-through this class exists to cover never runs. It read as an order-dependent
        # flake: the shard that materialised the file reddened `assert 'enabled' == 'paused'`
        # while the same test passed in isolation on a host.
        monkeypatch.setattr(cold_db, "canonical_data_dir", lambda **_: data_dir)
        monkeypatch.setattr(cold_db, "canonical_config_db", lambda **_: tmp_path / "absent" / "db.sqlite3")

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
    projection = HostProjection(
        generation=3,
        settings={GLOBAL_SCOPE: {"autoload": True}},
        loop_state={"dispatch": "enabled"},
        source="/var/lib/teatree/control-db/db.sqlite3",
        projected_at="2026-07-28T00:00:00+00:00",
    )

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


class TestHighwaterRatchetIsSerialised:
    """The observed-generation file may only move FORWARD, whatever the read interleaving.

    Two hooks read the projection at once; each compares its own generation against the
    file and then writes. Unlocked, the reader holding the OLDER generation can land its
    write after the newer one, leaving the file naming a generation this host has already
    passed — and the very next stale projection then reads FRESH.
    """

    def test_a_concurrent_recorder_waits_for_the_holder_and_then_skips(self, source_db: Path, data_dir: Path) -> None:
        reader = ProjectionReader(data_dir)
        ProjectionPublisher(source_db, data_dir).publish()
        assert reader.read().staleness is Staleness.FRESH, "control: the fresh projection must read clean"
        _plant_generation(reader.target, 5)
        reader.highwater_path.write_text("3\n", encoding="utf-8")

        with reader.highwater_lock_path.open("a", encoding="utf-8") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            late = threading.Thread(target=reader.read)
            late.start()
            late.join(timeout=0.5)
            assert late.is_alive(), "the generation-5 recorder did not serialise on the sibling lock"
            reader.highwater_path.write_text("9\n", encoding="utf-8")
            fcntl.flock(holder, fcntl.LOCK_UN)
        late.join(timeout=10)

        assert reader.highwater_path.read_text(encoding="utf-8").strip() == "9"

    def test_an_unwritable_data_dir_still_reads(self, source_db: Path, data_dir: Path) -> None:
        reader = ProjectionReader(data_dir)
        ProjectionPublisher(source_db, data_dir).publish()
        reader.highwater_lock_path.unlink()
        reader.highwater_path.write_text("1\n", encoding="utf-8")
        data_dir.chmod(0o500)
        try:
            assert reader.read().staleness is Staleness.FRESH
        finally:
            data_dir.chmod(0o700)
