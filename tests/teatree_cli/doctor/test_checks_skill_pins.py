"""The pin-freshness advisory reads the recorded measurement, offline, and never gates.

Doctor is the fast offline lane, so the comparison it reports was taken
elsewhere. What is pinned here is that reading a record can produce every answer
EXCEPT a quiet pass it did not earn: no record and an aged record are both
reported as unverified, a recorded suggestion renders a pasteable bump, and a
DECLARED pin the record never covered is unverified rather than absent.
"""

import datetime as dt
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from teatree.cli.doctor.checks_skill_pins import _check_skill_pin_freshness
from teatree.provisioning.skill_pin import PinAudit, SkillPinStatus, write_pin_audit

_SPEC = "team/skills/ac-python#d0008a39e9b1b9ba905e15380ee62ef459183dfd"
_MOVED_TO = "9f2c1ab4c5d6e7f8091a2b3c4d5e6f7a8b9c0d1e"
#: A whole-repo BUNDLE pin: two segments, so it names no single installable skill and
#: the skill enumeration drops it — the shape that went unmeasured in the real apm.yml.
_BUNDLE_SPEC = "obra/superpowers#1f20bef3f59b85ad7b52718f822e37c4478a3ff5"


@pytest.fixture
def manifest(tmp_path: Path) -> Path:
    """An ``apm.yml`` declaring exactly the pin the recorded fixtures measure.

    Stated per test rather than inherited from this checkout's own manifest, so these
    assertions describe the code's behaviour and not today's mandate list.
    """
    return _manifest_declaring(tmp_path, [_SPEC])


def _manifest_declaring(root: Path, entries: list[str]) -> Path:
    path = root / "apm.yml"
    body = "\n".join(f"  - {entry}" for entry in entries)
    path.write_text(f"name: team/thing\ndependencies:\n  apm:\n{body}\n", encoding="utf-8")
    return path


def _run(record_path: Path, *, now: dt.datetime | None = None, manifest: Path | None = None) -> tuple[bool, str]:
    holder: dict[str, bool] = {}
    app = typer.Typer()

    @app.command()
    def run() -> None:
        holder["ok"] = _check_skill_pin_freshness(record_path=record_path, now=now, manifest=manifest)

    result = CliRunner().invoke(app, [])
    return holder["ok"], result.output


def _record(path: Path, status: SkillPinStatus, *, measured_at: dt.datetime) -> None:
    write_pin_audit(PinAudit(measured_at=measured_at, statuses=(status,)), path)


def _status(**overrides: object) -> SkillPinStatus:
    fields: dict[str, object] = {
        "name": "ac-python",
        "spec": _SPEC,
        "pinned_ref": _SPEC.partition("#")[2],
        "head_sha": _SPEC.partition("#")[2],
        "branch": "main",
    }
    return SkillPinStatus(**(fields | overrides))


def test_no_recorded_measurement_is_unverified_not_up_to_date(tmp_path: Path) -> None:
    ok, output = _run(tmp_path / "absent.json")
    assert ok is True
    assert "UNVERIFIED" in output
    assert "up to date" not in output.lower()
    assert "FAIL" not in output


def test_recorded_current_pin_measured_recently_is_silent(tmp_path: Path, manifest: Path) -> None:
    now = dt.datetime(2026, 8, 4, tzinfo=dt.UTC)
    record = tmp_path / "audit.json"
    _record(record, _status(), measured_at=now - dt.timedelta(days=1))
    ok, output = _run(record, now=now, manifest=manifest)
    assert ok is True
    assert output.strip() == ""


def test_recorded_stale_pin_renders_a_pasteable_bump_and_gates_nothing(tmp_path: Path, manifest: Path) -> None:
    now = dt.datetime(2026, 8, 4, tzinfo=dt.UTC)
    record = tmp_path / "audit.json"
    _record(record, _status(head_sha=_MOVED_TO), measured_at=now - dt.timedelta(days=1))
    ok, output = _run(record, now=now, manifest=manifest)
    assert ok is True
    assert "INFO" in output
    assert f"apm install team/skills/ac-python#{_MOVED_TO}" in output
    assert "FAIL" not in output


def test_recorded_unmeasurable_pin_stays_unknown(tmp_path: Path, manifest: Path) -> None:
    now = dt.datetime(2026, 8, 4, tzinfo=dt.UTC)
    record = tmp_path / "audit.json"
    _record(record, _status(head_sha="", unmeasurable="no network"), measured_at=now - dt.timedelta(days=1))
    ok, output = _run(record, now=now, manifest=manifest)
    assert ok is True
    assert "UNVERIFIED" in output
    assert "no network" in output
    assert "up to date" not in output.lower()


def test_measurement_older_than_the_horizon_is_unverified_again(tmp_path: Path, manifest: Path) -> None:
    now = dt.datetime(2026, 8, 4, tzinfo=dt.UTC)
    record = tmp_path / "audit.json"
    _record(record, _status(), measured_at=now - dt.timedelta(days=90))
    ok, output = _run(record, now=now, manifest=manifest)
    assert ok is True
    assert "UNVERIFIED" in output
    assert "90 days" in output


class TestDeclaredPinCoverage:
    """A pin the record never covered must report UNVERIFIED, never read as clean (#4193).

    ``pin_advisory_lines`` speaks only about MEASURED pins, so a declared pin the
    measurement never reached produced no line at all — and a doctor that prints nothing
    is read as "every pin is current". The real ``apm.yml``'s only third-party pin is a
    two-segment bundle that the skill enumeration drops, so it was never measured and
    the check had been silently reporting on a strict subset of the mandate.
    """

    @staticmethod
    def _fresh_record(tmp_path: Path, now: dt.datetime) -> Path:
        record = tmp_path / "audit.json"
        _record(record, _status(), measured_at=now - dt.timedelta(days=1))
        return record

    def test_a_bundle_pin_the_measurement_never_covered_is_unverified(self, tmp_path: Path) -> None:
        now = dt.datetime(2026, 8, 4, tzinfo=dt.UTC)
        record = self._fresh_record(tmp_path, now)
        manifest = _manifest_declaring(tmp_path, [_SPEC, _BUNDLE_SPEC])

        ok, output = _run(record, now=now, manifest=manifest)

        assert ok is True
        assert "UNVERIFIED" in output
        assert _BUNDLE_SPEC in output
        assert "FAIL" not in output

    def test_a_pin_added_since_the_last_setup_is_unverified(self, tmp_path: Path) -> None:
        """The same hole from the other direction — the record is fresh but no longer complete."""
        now = dt.datetime(2026, 8, 4, tzinfo=dt.UTC)
        record = self._fresh_record(tmp_path, now)
        added = "team/skills/ac-rust#0123456789abcdef0123456789abcdef01234567"
        manifest = _manifest_declaring(tmp_path, [_SPEC, added])

        _ok, output = _run(record, now=now, manifest=manifest)

        assert added in output
        assert "UNVERIFIED" in output

    def test_an_unpinned_entry_is_not_reported(self, tmp_path: Path) -> None:
        """No ``#ref`` means no pin: it tracks whatever the source publishes, so nothing is owed."""
        now = dt.datetime(2026, 8, 4, tzinfo=dt.UTC)
        record = self._fresh_record(tmp_path, now)
        manifest = _manifest_declaring(tmp_path, [_SPEC, "souliane/teatree/skills/architecture-design"])

        _ok, output = _run(record, now=now, manifest=manifest)

        assert output.strip() == ""

    def test_an_unreadable_manifest_is_unverified_not_silent(self, tmp_path: Path) -> None:
        now = dt.datetime(2026, 8, 4, tzinfo=dt.UTC)
        record = self._fresh_record(tmp_path, now)
        broken = tmp_path / "apm.yml"
        broken.write_text("dependencies: [oh: no\n", encoding="utf-8")

        ok, output = _run(record, now=now, manifest=broken)

        assert ok is True
        assert "UNVERIFIED" in output
