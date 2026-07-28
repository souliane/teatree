"""Conformance: ONE source of truth for the active posture, and no second one can return (#3826).

The incident this pins: for a week the Django resolver said the owner was reachable
while the fast hooks obeyed a mirror file (``availability_override.json``) that still
said ``autonomous_away`` — 56 questions silently deferred with the owner at the
keyboard, and the doctor's stale-override alarm blind to it because it guarded the DB
row while the hooks read the file.

Three assertions, each of which would have gone RED on the pre-#3826 tree:

1.  :class:`TestResolversAgree` — the Django resolver and the Django-free cold
    resolver, given the SAME control DB, return the SAME posture across the whole
    precedence chain. This is the centrepiece: the old fast path had no DB to compare
    against, so the divergence was unrepresentable as a test at all.
2.  :class:`TestNoSecondPostureArtifact` — no shipped module names the retired mirror,
    a planted mirror changes nothing, and raw access to the override table is confined
    to the one cold reader. A future fast path cannot quietly reintroduce a file.
3.  :class:`TestFailsTowardAsking` — an unresolvable posture asks the user. The old
    design failed closed to the most restrictive posture, which is what muted the owner.
"""

# test-path: cross-cutting — spans the cold reader, the Django resolver and the shared
# live-presence leaf; no single mirror dir owns all three.

import datetime as dt
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from django.db import connection
from django.test import TestCase

from teatree import live_presence
from teatree.config import cold_mode
from teatree.core import mode_resolution
from teatree.core.models import ConfigSetting, Mode, ModeOverride, ModeSchedule, ModeScheduleSlot
from teatree.live_presence import PRESENCE_FILENAME, PresenceHeartbeat

_REPO = Path(__file__).resolve().parents[2]
_SHIPPED_ROOTS = (_REPO / "src", _REPO / "hooks", _REPO / "scripts", _REPO / "deploy")

#: The file the fast hooks used to obey. Named here so the scan is falsifiable — it is
#: the ONLY place in the tree the string may still appear.
RETIRED_MIRROR_FILENAME = "availability_override.json"

#: The one module allowed to read the override table outside the ORM + migrations: the
#: Django-free cold resolver. Every other reader goes through `ModeOverride`.
COLD_OVERRIDE_READER = "src/teatree/config/cold_mode.py"

_OVERRIDE_TABLE = ModeOverride._meta.db_table

_DUMP_SCHEMA = """
CREATE TABLE teatree_loop_preset (name TEXT, defers_questions INT, pauses_self_pump INT, presence_sensitive INT);
CREATE TABLE teatree_loop_preset_override (preset_name TEXT, until TEXT, set_at TEXT);
CREATE TABLE teatree_loop_schedule (id INT, name TEXT, timezone TEXT);
CREATE TABLE teatree_loop_schedule_slot (schedule_id INT, days TEXT, start_time TEXT, preset_name TEXT);
CREATE TABLE teatree_config_setting (scope TEXT, key TEXT, value TEXT);
"""

#: Every posture table, as a literal read/write statement pair. Written out rather
#: than composed from `_meta.db_table` so no SQL string is ever built at runtime.
_DUMP_STATEMENTS = (
    (
        "SELECT name, defers_questions, pauses_self_pump, presence_sensitive FROM teatree_loop_preset",
        "INSERT INTO teatree_loop_preset VALUES (?, ?, ?, ?)",
    ),
    (
        "SELECT preset_name, until, set_at FROM teatree_loop_preset_override",
        "INSERT INTO teatree_loop_preset_override VALUES (?, ?, ?)",
    ),
    (
        "SELECT id, name, timezone FROM teatree_loop_schedule",
        "INSERT INTO teatree_loop_schedule VALUES (?, ?, ?)",
    ),
    (
        "SELECT schedule_id, days, start_time, preset_name FROM teatree_loop_schedule_slot",
        "INSERT INTO teatree_loop_schedule_slot VALUES (?, ?, ?, ?)",
    ),
    (
        "SELECT scope, key, value FROM teatree_config_setting",
        "INSERT INTO teatree_config_setting VALUES (?, ?, ?)",
    ),
)

NOW = dt.datetime(2026, 7, 28, 12, 0, tzinfo=dt.UTC)


def _as_stored(value: object) -> object:
    """Re-serialize a value Django's sqlite converters rehydrated, back to its column text.

    ``SELECT start_time`` hands back a ``datetime.time`` because Django registers
    converters on its own connection; the cold reader sees the raw column. Writing the
    same text back is what keeps the dump faithful to what is actually on disk. A
    rehydrated datetime comes back NAIVE and already in UTC (that is how Django stores
    it), so it is stamped rather than converted — ``astimezone`` on a naive value would
    reinterpret it in the process timezone and shift every expiry.
    """
    if isinstance(value, dt.datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)
        return aware.astimezone(dt.UTC).replace(tzinfo=None).isoformat(sep=" ")
    if isinstance(value, dt.time):
        return value.isoformat()
    return value


def _dump_control_db(target: Path) -> None:
    """Copy the live posture tables verbatim into a standalone sqlite file.

    Rows are read through the SAME connection Django resolves against and written
    byte-for-byte, so the parity assertion compares the two RESOLVERS rather than two
    hand-written fixtures that could drift apart and hide a real divergence.
    """
    cold = sqlite3.connect(target)
    try:
        cold.executescript(_DUMP_SCHEMA)
        with connection.cursor() as cursor:
            for select, insert in _DUMP_STATEMENTS:
                cursor.execute(select)
                cold.executemany(insert, [tuple(_as_stored(value) for value in row) for row in cursor.fetchall()])
        cold.commit()
    finally:
        cold.close()


#: The three shipped postures. Upserted (not created) because the mode rows may already
#: exist from the seed; the test owns their posture, not their existence.
_POSTURES = {
    "engaged": (False, False, True),
    "unattended": (True, False, True),
    "offline": (True, True, False),
}


def _seed_modes() -> None:
    for name, (defers, pauses, presence_sensitive) in _POSTURES.items():
        Mode.objects.update_or_create(
            name=name,
            defaults={
                "defers_questions": defers,
                "pauses_self_pump": pauses,
                "presence_sensitive": presence_sensitive,
            },
        )


def _seed_schedule(preset_name: str) -> None:
    schedule = ModeSchedule.objects.create(name="standard", timezone="UTC")
    ModeScheduleSlot.objects.create(
        schedule=schedule, days=[0, 1, 2, 3, 4, 5, 6], start_time=dt.time(0, 0), preset_name=preset_name
    )
    ConfigSetting.objects.set_value("active_loop_schedule", value="standard")


class _PostureCase(TestCase):
    """Shared harness: seed the ORM, dump it, resolve both sides against the one state."""

    def setUp(self) -> None:
        super().setUp()
        # A seeded install ships an active schedule + a mode set; each case declares its
        # own layers, so start from a control plane with no override and no calendar.
        ModeOverride.objects.all().delete()
        ModeScheduleSlot.objects.all().delete()
        ModeSchedule.objects.all().delete()
        ConfigSetting.objects.filter(key__in=("active_loop_schedule", "default_mode")).delete()
        self.data_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.heartbeat = PresenceHeartbeat(locate=lambda: self.data_dir / PRESENCE_FILENAME)
        self.addCleanup(setattr, mode_resolution, "PRESENCE", live_presence.PRESENCE)
        mode_resolution.PRESENCE = self.heartbeat

    def keystroke(self, at: dt.datetime) -> None:
        self.heartbeat.record(session_id="s", now=at)

    def postures(self, now: dt.datetime = NOW) -> tuple[tuple[bool, bool], tuple[bool, bool]]:
        """``(django, cold)`` postures resolved from the identical control-DB state."""
        db = self.data_dir / "dump.sqlite3"
        db.unlink(missing_ok=True)
        _dump_control_db(db)
        resolved = mode_resolution.resolve_active_mode(now)
        cold = cold_mode.resolve_cold_posture(now, db_path=db, data_dir=self.data_dir)
        return (resolved.defers_questions, resolved.pauses_self_pump), (cold.defers_questions, cold.pauses_self_pump)

    def assert_agree(self, expected: tuple[bool, bool], now: dt.datetime = NOW) -> None:
        django_posture, cold_posture = self.postures(now)
        assert django_posture == cold_posture, f"resolvers disagree: django={django_posture} cold={cold_posture}"
        assert django_posture == expected


class TestResolversAgree(_PostureCase):
    """The Django resolver and the cold resolver never disagree on the same DB state."""

    def test_empty_control_plane_is_reachable(self) -> None:
        self.assert_agree((False, False))

    def test_no_override_row_at_all(self) -> None:
        # The exact state the box was in on 2026-07-21..28: the override table EMPTY.
        # The mirror said `autonomous_away` and the hooks obeyed it for a week; with one
        # source of truth there is nothing left to say it.
        _seed_modes()
        ConfigSetting.objects.set_value("default_mode", value="engaged")
        assert not ModeOverride.objects.exists()
        self.assert_agree((False, False))

    def test_configured_default_mode(self) -> None:
        _seed_modes()
        ConfigSetting.objects.set_value("default_mode", value="unattended")
        self.assert_agree((True, False))

    def test_manual_override_to_the_holiday_mode(self) -> None:
        _seed_modes()
        ModeOverride.objects.set_override("offline")
        self.assert_agree((True, True))

    def test_expired_override_falls_through_to_the_schedule(self) -> None:
        _seed_modes()
        _seed_schedule("engaged")
        ModeOverride.objects.set_override("offline", until=NOW - dt.timedelta(hours=1))
        self.assert_agree((False, False))

    def test_schedule_slot_governs(self) -> None:
        _seed_modes()
        _seed_schedule("unattended")
        self.assert_agree((True, False))

    def test_fresh_keystroke_upgrades_a_scheduled_away_mode(self) -> None:
        _seed_modes()
        _seed_schedule("unattended")
        self.keystroke(NOW - dt.timedelta(minutes=1))
        self.assert_agree((False, False))

    def test_a_keystroke_never_overrides_a_manual_override(self) -> None:
        # The deliberate rule in `_apply_presence_upgrade`, pinned on BOTH readers: a
        # human who said "defer my questions" is not overruled by typing. Only the
        # mirror MASQUERADING as an override was ever wrong.
        _seed_modes()
        ModeOverride.objects.set_override("unattended")
        self.keystroke(NOW)
        self.assert_agree((True, False))

    def test_override_naming_a_deleted_mode(self) -> None:
        _seed_modes()
        _seed_schedule("offline")
        ModeOverride.objects.set_override("deleted-mode")
        self.assert_agree((False, False))

    def test_active_schedule_naming_an_unknown_calendar(self) -> None:
        _seed_modes()
        ConfigSetting.objects.set_value("active_loop_schedule", value="no-such-calendar")
        self.assert_agree((False, False))

    def test_setting_names_are_shared_by_both_readers(self) -> None:
        # Agreement on VALUES is worthless if the two read different keys, and a key
        # typo is invisible in a green matrix (both would fall back to the default).
        assert cold_mode.DEFAULT_MODE_SETTING == mode_resolution.DEFAULT_MODE_SETTING
        assert cold_mode.PRESENCE_UPGRADE_SETTING == mode_resolution.PRESENCE_UPGRADE_SETTING
        assert cold_mode.FALLBACK_DEFAULT_MODE == mode_resolution.FALLBACK_DEFAULT_MODE
        assert cold_mode.FALLBACK_UPGRADE_MODE == mode_resolution.FALLBACK_UPGRADE_MODE

    def test_cold_schedule_zone_fallback_matches_the_project_timezone(self) -> None:
        # The cold reader cannot read Django settings, so it hardcodes the project zone
        # for a schedule with no explicit timezone. Pin the source they must agree on.
        settings_source = (_REPO / "src" / "teatree" / "settings.py").read_text(encoding="utf-8")
        assert 'TIME_ZONE = "UTC"' in settings_source


class TestNoSecondPostureArtifact(_PostureCase):
    """No posture may be read from an on-disk artifact, and none may be reintroduced."""

    def test_no_shipped_module_names_the_retired_mirror(self) -> None:
        offenders = [
            str(path.relative_to(_REPO))
            for root in _SHIPPED_ROOTS
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix in {".py", ".sh", ".toml", ".html"}
            and RETIRED_MIRROR_FILENAME in path.read_text(encoding="utf-8", errors="ignore")
        ]
        assert offenders == [], f"the retired posture mirror is named again in: {offenders}"

    def test_a_planted_mirror_changes_nothing(self) -> None:
        # The control: with the DB saying reachable, dropping the exact file the old
        # fast path obeyed — holding the exact value that muted the owner — must not
        # move the posture by one bit.
        _seed_modes()
        ConfigSetting.objects.set_value("default_mode", value="engaged")
        before = self.postures()
        (self.data_dir / RETIRED_MIRROR_FILENAME).write_text('{"mode": "autonomous_away"}', encoding="utf-8")
        assert self.postures() == before == ((False, False), (False, False))

    def test_raw_override_table_access_is_confined_to_the_cold_reader(self) -> None:
        # Every other consumer reads the row through the `ModeOverride` ORM model, so a
        # new hand-rolled reader (the shape that produced the mirror) shows up here.
        readers = sorted(
            str(path.relative_to(_REPO))
            for root in _SHIPPED_ROOTS
            for path in root.rglob("*.py")
            if _OVERRIDE_TABLE in path.read_text(encoding="utf-8", errors="ignore")
            and "migrations" not in path.parts
            and "models" not in path.parts
        )
        assert readers == [COLD_OVERRIDE_READER], f"unexpected raw reader of {_OVERRIDE_TABLE}: {readers}"


class TestFailsTowardAsking(_PostureCase):
    """An unresolvable posture interrupts the user; it never silences them."""

    def test_unreadable_control_db_resolves_reachable(self) -> None:
        posture = cold_mode.resolve_cold_posture(NOW, db_path=self.data_dir / "absent.sqlite3")
        assert posture.defers_questions is False
        assert posture.pauses_self_pump is False

    def test_the_probe_asks_when_teatree_is_unimportable(self) -> None:
        from hooks.scripts import mode_posture_probe  # noqa: PLC0415 — hook module, imported at use

        @contextmanager
        def boom() -> Iterator[None]:
            raise ModuleNotFoundError
            yield  # pragma: no cover — unreachable; satisfies the generator contract

        with mock.patch.object(mode_posture_probe, "teatree_src_on_path", boom):
            assert mode_posture_probe.resolved_defers_questions() is False
            assert mode_posture_probe.resolved_pauses_self_pump() is False
