"""The one path in the MCP liveness gate that MUTATES the operator's machine (#4049).

Every branch is driven for real here — the attempted-guard, the unresolvable plan, the
subprocess spawn, both failure branches and the re-verify — because the guard's first
spelling shipped patched out of both tests that reached it: the reinstall it performs
replaces the console script the calling session is running under, so an untested branch
here is an untested `uv tool install --reinstall` on the host.

The already-attempted guard gets its own class. Its first spelling wrote to the running
process's ``os.environ`` and read it back inside the SAME one-call-per-process function,
so it was unreachable in production and an unrepairable skew reinstalled forever. These
pin the property the docstring claims: the record must survive the process that wrote it.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.cli.dep_drift_repair import RepairPlan
from teatree.cli.doctor.skew_repair import (
    clear_repair_receipt,
    receipt_path,
    record_repair_attempt,
    repair_already_attempted,
    repair_version_skew,
    report_version_skew,
    skew_fingerprint,
)
from teatree.utils.dep_skew import VersionSkew
from teatree.utils.run import CompletedProcess

_SKEW = [VersionSkew(name="mcp", declared=">=2,<3", installed="1.28.1")]
_OTHER_SKEW = [VersionSkew(name="typer", declared=">=1", installed="0.9.0")]
_PLAN = RepairPlan(cmd=["uv", "tool", "install", "--editable", "."], label="uv tool install --editable .")


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the receipt at a scratch dir — it is a real file, written for real."""
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def source(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("[project]\ndependencies = []\n", encoding="utf-8")
    return tmp_path


def _ran(returncode: int, stderr: str = "") -> CompletedProcess[str]:
    return CompletedProcess(args=["uv"], returncode=returncode, stdout="", stderr=stderr)


def _plan(resolved: RepairPlan | str = _PLAN):
    return patch("teatree.cli.dep_drift_repair.resolve_repair_plan", return_value=resolved)


def _spawn(result: CompletedProcess[str]):
    return patch("teatree.utils.run.run_allowed_to_fail", return_value=result)


def _remaining(skews: list[VersionSkew]):
    return patch("teatree.utils.dep_skew.find_version_skew", return_value=skews)


class TestTheRepairSpawn:
    def test_it_runs_the_resolved_plan_and_reports_success_once_the_skew_is_gone(
        self, data_dir: Path, source: Path, capsys
    ) -> None:
        with _plan(), _spawn(_ran(0)) as spawn, _remaining([]):
            assert repair_version_skew(source, _SKEW) is True

        assert spawn.call_args.args[0] == _PLAN.cmd, "the plan's command is what must actually run"
        assert "Repaired" in capsys.readouterr().out

    def test_a_nonzero_exit_fails_and_hands_back_the_manual_command(self, data_dir: Path, source: Path, capsys) -> None:
        with _plan(), _spawn(_ran(1, stderr="No solution found")), _remaining([]):
            assert repair_version_skew(source, _SKEW) is False

        out = capsys.readouterr().out
        assert "Repair FAILED: No solution found" in out
        assert f"Manual fix: `{_PLAN.label}`" in out

    def test_a_repair_that_runs_but_leaves_the_skew_is_a_failure(self, data_dir: Path, source: Path, capsys) -> None:
        """The re-verify: a clean exit code is not evidence the env is current."""
        with _plan(), _spawn(_ran(0)), _remaining(_SKEW):
            assert repair_version_skew(source, _SKEW) is False

        assert "Repair ran but skew persists: mcp declares" in capsys.readouterr().out

    def test_an_unresolvable_plan_fails_without_spawning_anything(self, data_dir: Path, source: Path, capsys) -> None:
        with _plan("WARN  Teatree is installed non-editable"), _spawn(_ran(0)) as spawn:
            assert repair_version_skew(source, _SKEW) is False

        assert spawn.call_count == 0
        assert "Cannot self-repair: WARN  Teatree is installed non-editable" in capsys.readouterr().out


class TestTheAttemptedGuardSurvivesTheProcess:
    def test_a_second_run_after_an_ineffective_repair_reports_instead_of_reinstalling(
        self, data_dir: Path, source: Path, capsys
    ) -> None:
        """The whole point: the marker must outlive the process that wrote it."""
        with _plan(), _spawn(_ran(0)), _remaining(_SKEW):
            repair_version_skew(source, _SKEW)
        capsys.readouterr()

        with _plan(), _spawn(_ran(0)) as spawn, _remaining(_SKEW):
            assert repair_version_skew(source, _SKEW) is False

        assert spawn.call_count == 0, "an unrepairable skew must not reinstall on every doctor run"
        out = capsys.readouterr().out
        assert "already ran and it persists" in out
        assert "Fix it by running: `" in out, "the operator still gets the command to run by hand"

    def test_the_receipt_is_a_file_written_by_the_repairing_process(self, data_dir: Path, source: Path) -> None:
        """A control-DB row cannot serve this: the DB is a docker volume host `t3` cannot open."""
        with _plan(), _spawn(_ran(0)), _remaining(_SKEW):
            repair_version_skew(source, _SKEW)

        record = json.loads(receipt_path().read_text(encoding="utf-8"))
        assert receipt_path().parent == data_dir, "each venue records its own install's attempt"
        assert record["fingerprint"] == skew_fingerprint(_SKEW)

    def test_a_different_skew_is_still_repaired_once(self, data_dir: Path, source: Path) -> None:
        """Keyed on the drift, so fresh drift is never blocked by an old receipt."""
        record_repair_attempt(skew_fingerprint(_SKEW))

        with _plan(), _spawn(_ran(0)) as spawn, _remaining([]):
            assert repair_version_skew(source, _OTHER_SKEW) is True

        assert spawn.call_count == 1

    def test_a_successful_repair_drops_the_receipt_it_claimed(self, data_dir: Path, source: Path) -> None:
        """Otherwise the same drift recurring later would be refused a repair forever."""
        with _plan(), _spawn(_ran(0)), _remaining([]):
            assert repair_version_skew(source, _SKEW) is True

        assert not receipt_path().exists()

    def test_the_attempt_is_claimed_before_the_reinstall_runs(self, data_dir: Path, source: Path) -> None:
        """A reinstall killed mid-flight still counts — it may have half-replaced the env."""
        seen: list[bool] = []

        def record_then_die(*_args: object, **_kwargs: object) -> CompletedProcess[str]:
            seen.append(repair_already_attempted(skew_fingerprint(_SKEW)))
            return _ran(1)

        with _plan(), patch("teatree.utils.run.run_allowed_to_fail", record_then_die), _remaining([]):
            repair_version_skew(source, _SKEW)

        assert seen == [True]

    def test_an_unreadable_receipt_does_not_block_the_repair_the_operator_asked_for(
        self, data_dir: Path, source: Path
    ) -> None:
        receipt_path().write_text("{ not json", encoding="utf-8")

        assert repair_already_attempted(skew_fingerprint(_SKEW)) is False

    def test_clearing_a_receipt_that_was_never_written_is_not_an_error(self, data_dir: Path) -> None:
        clear_repair_receipt()

        assert not receipt_path().exists()


class TestTheReadOnlyReport:
    def test_it_prints_a_pasteable_command_and_the_flag_that_runs_it(self, data_dir: Path, capsys) -> None:
        with _plan():
            report_version_skew(_SKEW)

        out = capsys.readouterr().out
        assert f"Fix it by running: `{_PLAN.label}`" in out
        assert "t3 doctor check --repair" in out

    def test_an_install_with_no_self_repair_says_so_rather_than_printing_a_broken_command(
        self, data_dir: Path, capsys
    ) -> None:
        with _plan("WARN  Teatree is installed non-editable"):
            report_version_skew(_SKEW)

        assert "No self-repair applies" in capsys.readouterr().out


class TestTheFingerprint:
    def test_it_is_stable_across_ordering(self) -> None:
        both = [*_SKEW, *_OTHER_SKEW]

        assert skew_fingerprint(both) == skew_fingerprint(list(reversed(both)))

    def test_a_different_installed_version_is_a_different_drift(self) -> None:
        moved = [VersionSkew(name="mcp", declared=">=2,<3", installed="1.29.0")]

        assert skew_fingerprint(_SKEW) != skew_fingerprint(moved)
