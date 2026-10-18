import datetime as dt
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner, Result

from teatree.cli import app
from teatree.eval.discovery import discover_specs
from teatree.eval.owed import LEDGER_ARTIFACT, PAGE_SIZE, OwedLedger, OwedReason

REPO = "souliane/teatree"
NOW = "2026-10-16T03:00:00+00:00"


def _owed(*args: str) -> Result:
    return CliRunner().invoke(app, ["eval", "owed", *args])


def _names_file(tmp_path: Path, *names: str, filename: str = "names.txt") -> Path:
    path = tmp_path / filename
    path.write_text("".join(f"{name}\n" for name in names), encoding="utf-8")
    return path


def _record(ledger: Path, names: Path, reason: str, *, now: str = NOW) -> Result:
    return _owed(
        "record",
        "--out",
        str(ledger),
        "--reason",
        reason,
        "--source",
        "pr#7@abc",
        "--names-file",
        str(names),
        "--now",
        now,
    )


def _scenarios_dir(tmp_path: Path, *names: str) -> Path:
    directory = tmp_path / "scenarios"
    directory.mkdir()
    for name in names:
        (directory / f"{name}.yaml").write_text(
            f"- name: {name}\n"
            f"  scenario: {name} scenario\n"
            "  prompt: do the thing\n"
            "  expect:\n"
            "    - tool_call: bash\n"
            '      args.command: contains "x"\n',
            encoding="utf-8",
        )
    return directory


def _seeded_ledger(tmp_path: Path) -> Path:
    ledger = tmp_path / "eval-owed-ledger.json"
    assert _record(ledger, _names_file(tmp_path, "beta", "gone", filename="new.txt"), "limit").exit_code == 0
    older = _names_file(tmp_path, "alpha", filename="old.txt")
    assert _record(ledger, older, "deferred", now="2026-10-10T00:00:00+00:00").exit_code == 0
    return ledger


class TestRecordAndOutputs:
    def test_parked_names_round_trip_to_the_step_outputs(self, tmp_path: Path) -> None:
        ledger = tmp_path / "eval-owed.json"
        recorded = _record(ledger, _names_file(tmp_path, "beta", "", "alpha"), "parked")
        assert recorded.exit_code == 0, recorded.output
        assert "recorded 2 owed scenario(s) (parked)" in recorded.output

        outputs = _owed("outputs", str(ledger))
        assert outputs.exit_code == 0, outputs.output
        assert outputs.stdout == "parked=true\nowed=alpha,beta\n"

    def test_deferred_only_is_owed_but_not_parked(self, tmp_path: Path) -> None:
        ledger = tmp_path / "eval-owed.json"
        assert _record(ledger, _names_file(tmp_path, "tail_one"), "deferred").exit_code == 0
        assert _owed("outputs", str(ledger)).stdout == "parked=false\nowed=tail_one\n"

    def test_nothing_to_record_writes_an_empty_artifact(self, tmp_path: Path) -> None:
        ledger = tmp_path / "eval-owed.json"
        result = _record(ledger, _names_file(tmp_path, "", filename="empty.txt"), "limit")
        assert result.exit_code == 0, result.output
        assert "nothing owed (limit)" in result.output
        assert OwedLedger.from_json(ledger.read_text(encoding="utf-8")) == OwedLedger()

    def test_a_second_record_adds_to_the_ledger(self, tmp_path: Path) -> None:
        ledger = tmp_path / "eval-owed.json"
        assert _record(ledger, _names_file(tmp_path, "tail_one", filename="tail.txt"), "deferred").exit_code == 0
        assert _record(ledger, _names_file(tmp_path, "hit_limit", filename="limit.txt"), "limit").exit_code == 0
        assert _owed("outputs", str(ledger)).stdout == "parked=true\nowed=hit_limit,tail_one\n"
        recorded = OwedLedger.from_json(ledger.read_text(encoding="utf-8"))
        assert {entry.reason for entry in recorded.scenarios} == {OwedReason.DEFERRED, OwedReason.LIMIT}
        assert {entry.since.isoformat() for entry in recorded.scenarios} == {NOW}

    def test_without_now_the_debt_is_dated_at_the_clock(self, tmp_path: Path) -> None:
        ledger = tmp_path / "eval-owed.json"
        before = dt.datetime.now(dt.UTC)
        result = _owed(
            "record",
            "--out",
            str(ledger),
            "--reason",
            "limit",
            "--source",
            "nightly@1",
            "--names-file",
            str(_names_file(tmp_path, "a")),
        )
        assert result.exit_code == 0, result.output
        (entry,) = OwedLedger.from_json(ledger.read_text(encoding="utf-8")).scenarios
        assert before <= entry.since <= dt.datetime.now(dt.UTC)

    def test_a_missing_ledger_reports_nothing_owed(self, tmp_path: Path) -> None:
        result = _owed("outputs", str(tmp_path / "never-written.json"))
        assert result.exit_code == 0, result.output
        assert result.stdout == "parked=false\nowed=\n"

    def test_a_missing_names_file_exits_two(self, tmp_path: Path) -> None:
        result = _record(tmp_path / "eval-owed.json", tmp_path / "absent.txt", "parked")
        assert result.exit_code == 2
        assert "--names-file" in result.stderr
        assert not (tmp_path / "eval-owed.json").exists()

    def test_an_unparsable_now_exits_two(self, tmp_path: Path) -> None:
        result = _record(tmp_path / "eval-owed.json", _names_file(tmp_path, "alpha"), "parked", now="tomorrow")
        assert result.exit_code == 2
        assert "--now" in result.stderr

    def test_a_malformed_ledger_fails_loud(self, tmp_path: Path) -> None:
        ledger = tmp_path / "eval-owed.json"
        ledger.write_text('{"version": 9}', encoding="utf-8")
        result = _owed("outputs", str(ledger))
        assert result.exit_code == 1
        assert "version" in result.stderr
        assert "parked=" not in result.stdout


class TestNext:
    def test_prints_the_oldest_known_debt_first_up_to_the_cap(self, tmp_path: Path) -> None:
        ledger = _seeded_ledger(tmp_path)
        scenarios = _scenarios_dir(tmp_path, "alpha", "beta")
        result = _owed("next", str(ledger), "--cap", "1", "--scenarios-dir", str(scenarios))
        assert result.exit_code == 0, result.output
        assert result.stdout == "alpha\n"
        assert "gone" in result.stderr

        wide = _owed("next", str(ledger), "--cap", "14", "--scenarios-dir", str(scenarios))
        assert wide.stdout == "alpha\nbeta\n"

    def test_a_missing_ledger_prints_nothing(self, tmp_path: Path) -> None:
        result = _owed("next", str(tmp_path / "absent.json"), "--cap", "14")
        assert result.exit_code == 0, result.output
        assert result.stdout == ""

    def test_the_default_catalog_is_the_discovered_one(self, tmp_path: Path) -> None:
        real = min(spec.name for spec in discover_specs())
        ledger = tmp_path / "eval-owed-ledger.json"
        assert _record(ledger, _names_file(tmp_path, real, "no_such_scenario_anywhere"), "parked").exit_code == 0
        result = _owed("next", str(ledger), "--cap", "14")
        assert result.exit_code == 0, result.output
        assert result.stdout == f"{real}\n"
        assert "no_such_scenario_anywhere" in result.stderr

    def test_a_cap_below_one_is_a_usage_error(self, tmp_path: Path) -> None:
        result = _owed("next", str(_seeded_ledger(tmp_path)), "--cap", "0")
        assert result.exit_code == 2
        assert "--cap" in result.stderr

    def test_an_empty_scenarios_dir_refuses(self, tmp_path: Path) -> None:
        result = _owed("next", str(_seeded_ledger(tmp_path)), "--cap", "1", "--scenarios-dir", str(tmp_path / "nope"))
        assert result.exit_code == 2
        assert "no eval scenarios" in result.stderr


class TestSettle:
    def test_a_complete_green_benchmark_clears_the_reusable_ledger(self, tmp_path: Path) -> None:
        ledger = _seeded_ledger(tmp_path)
        result = _owed("settle", "--out", str(ledger), "--all-covered")
        assert result.exit_code == 0, result.output
        assert OwedLedger.from_json(ledger.read_text(encoding="utf-8")).names == ()

    def test_clears_passes_keeps_failures_and_drops_catalog_unknown(self, tmp_path: Path) -> None:
        ledger = _seeded_ledger(tmp_path)
        scenarios = _scenarios_dir(tmp_path, "alpha", "beta")
        passed = _names_file(tmp_path, "alpha", filename="passed.txt")
        result = _owed("settle", "--out", str(ledger), "--passed-file", str(passed), "--scenarios-dir", str(scenarios))
        assert result.exit_code == 0, result.output
        assert "gone" in result.stderr
        settled = OwedLedger.from_json(ledger.read_text(encoding="utf-8"))
        assert settled.names == ("beta",)

    def test_a_missing_passed_file_fails_loud(self, tmp_path: Path) -> None:
        ledger = _seeded_ledger(tmp_path)
        before = ledger.read_text(encoding="utf-8")
        result = _owed("settle", "--out", str(ledger), "--passed-file", str(tmp_path / "absent.txt"))
        assert result.exit_code == 2
        assert "--passed-file" in result.stderr
        assert ledger.read_text(encoding="utf-8") == before

    def test_a_missing_ledger_fails_loud(self, tmp_path: Path) -> None:
        passed = _names_file(tmp_path, "alpha")
        result = _owed("settle", "--out", str(tmp_path / "absent.json"), "--passed-file", str(passed))
        assert result.exit_code == 2
        assert "no owed ledger" in result.stderr
        assert not (tmp_path / "absent.json").exists()

    def test_a_degraded_catalog_drops_nothing_it_cannot_see(self, tmp_path: Path) -> None:
        ledger = _seeded_ledger(tmp_path)
        passed = _names_file(tmp_path, "alpha", filename="passed.txt")
        with patch("teatree.core.overlay_loader.get_all_overlays", side_effect=RuntimeError("registry down")):
            result = _owed("settle", "--out", str(ledger), "--passed-file", str(passed))
        assert result.exit_code == 0, result.output
        assert "degraded" in result.stderr
        assert OwedLedger.from_json(ledger.read_text(encoding="utf-8")).names == ("beta", "gone")


def _fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, routes: dict[str, bytes]) -> None:
    bodies = tmp_path / "gh-bodies"
    bodies.mkdir()
    table: dict[str, str] = {}
    for index, (endpoint, body) in enumerate(routes.items()):
        path = bodies / f"{index}.bin"
        path.write_bytes(body)
        table[f"api {endpoint}"] = str(path)
    table_path = tmp_path / "gh-routes.json"
    table_path.write_text(json.dumps(table), encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "gh"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        f"routes = json.loads(Path({str(table_path)!r}).read_text())\n"
        "key = ' '.join(sys.argv[1:])\n"
        "if key not in routes:\n"
        "    sys.stderr.write(f'HTTP 502: {key}\\n')\n"
        "    sys.exit(1)\n"
        "sys.stdout.buffer.write(Path(routes[key]).read_bytes())\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def _first_page(endpoint: str) -> str:
    return f"repos/{REPO}/{endpoint}&per_page={PAGE_SIZE}&page=1"


@pytest.mark.integration
class TestCollect:
    def test_a_first_harvest_writes_an_empty_ledger_marked_now(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_gh(
            tmp_path,
            monkeypatch,
            {
                _first_page(f"actions/artifacts?name={LEDGER_ARTIFACT}"): b'{"total_count": 0, "artifacts": []}',
                _first_page("pulls?state=closed&sort=updated&direction=desc"): b"[]",
            },
        )
        out = tmp_path / "eval-owed-ledger.json"
        result = _owed("collect", "--repo", REPO, "--out", str(out), "--now", NOW)
        assert result.exit_code == 0, result.output
        assert "first harvest" in result.stdout
        assert "collected 0 owed scenario(s)" in result.stdout
        collected = OwedLedger.from_json(out.read_text(encoding="utf-8"))
        assert collected == OwedLedger(harvested_through=collected.harvested_through)
        assert collected.harvested_through is not None
        assert collected.harvested_through.isoformat() == NOW

    def test_a_github_api_error_exits_one_and_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_gh(tmp_path, monkeypatch, {})
        out = tmp_path / "eval-owed-ledger.json"
        result = _owed("collect", "--repo", REPO, "--out", str(out), "--now", NOW)
        assert result.exit_code == 1
        assert "eval owed collect failed" in result.stderr
        assert not out.exists()

    def test_an_unparsable_payload_exits_one(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_gh(tmp_path, monkeypatch, {_first_page(f"actions/artifacts?name={LEDGER_ARTIFACT}"): b"<html>"})
        result = _owed("collect", "--repo", REPO, "--out", str(tmp_path / "out.json"), "--now", NOW)
        assert result.exit_code == 1
        assert "not valid JSON" in result.stderr
