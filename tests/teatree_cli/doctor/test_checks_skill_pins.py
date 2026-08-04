"""The pin-freshness advisory reads the recorded measurement, offline, and never gates.

Doctor is the fast offline lane, so the comparison it reports was taken
elsewhere. What is pinned here is that reading a record can produce every answer
EXCEPT a quiet pass it did not earn: no record and an aged record are both
reported as unverified, and a recorded suggestion renders a pasteable bump.
"""

import datetime as dt
from pathlib import Path

import typer
from typer.testing import CliRunner

from teatree.cli.doctor.checks_skill_pins import _check_skill_pin_freshness
from teatree.provisioning.skill_pin import PinAudit, SkillPinStatus, write_pin_audit

_SPEC = "team/skills/ac-python#d0008a39e9b1b9ba905e15380ee62ef459183dfd"
_MOVED_TO = "9f2c1ab4c5d6e7f8091a2b3c4d5e6f7a8b9c0d1e"


def _run(record_path: Path, *, now: dt.datetime | None = None) -> tuple[bool, str]:
    holder: dict[str, bool] = {}
    app = typer.Typer()

    @app.command()
    def run() -> None:
        holder["ok"] = _check_skill_pin_freshness(record_path=record_path, now=now)

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
    return SkillPinStatus(**(fields | overrides))  # type: ignore[arg-type]


def test_no_recorded_measurement_is_unverified_not_up_to_date(tmp_path: Path) -> None:
    ok, output = _run(tmp_path / "absent.json")
    assert ok is True
    assert "UNVERIFIED" in output
    assert "up to date" not in output.lower()
    assert "FAIL" not in output


def test_recorded_current_pin_measured_recently_is_silent(tmp_path: Path) -> None:
    now = dt.datetime(2026, 8, 4, tzinfo=dt.UTC)
    record = tmp_path / "audit.json"
    _record(record, _status(), measured_at=now - dt.timedelta(days=1))
    ok, output = _run(record, now=now)
    assert ok is True
    assert output.strip() == ""


def test_recorded_stale_pin_renders_a_pasteable_bump_and_gates_nothing(tmp_path: Path) -> None:
    now = dt.datetime(2026, 8, 4, tzinfo=dt.UTC)
    record = tmp_path / "audit.json"
    _record(record, _status(head_sha=_MOVED_TO), measured_at=now - dt.timedelta(days=1))
    ok, output = _run(record, now=now)
    assert ok is True
    assert "INFO" in output
    assert f"apm install team/skills/ac-python#{_MOVED_TO}" in output
    assert "FAIL" not in output


def test_recorded_unmeasurable_pin_stays_unknown(tmp_path: Path) -> None:
    now = dt.datetime(2026, 8, 4, tzinfo=dt.UTC)
    record = tmp_path / "audit.json"
    _record(record, _status(head_sha="", unmeasurable="no network"), measured_at=now - dt.timedelta(days=1))
    ok, output = _run(record, now=now)
    assert ok is True
    assert "UNVERIFIED" in output
    assert "no network" in output
    assert "up to date" not in output.lower()


def test_measurement_older_than_the_horizon_is_unverified_again(tmp_path: Path) -> None:
    now = dt.datetime(2026, 8, 4, tzinfo=dt.UTC)
    record = tmp_path / "audit.json"
    _record(record, _status(), measured_at=now - dt.timedelta(days=90))
    ok, output = _run(record, now=now)
    assert ok is True
    assert "UNVERIFIED" in output
    assert "90 days" in output
