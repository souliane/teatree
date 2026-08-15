"""Tests for the dependency-audit canary (souliane/teatree#4346).

The canary exists because a green audit is indistinguishable from a blind one:
Django 6.0.8 shipped four CVEs on 2026-08-04 and every audit lane stayed green
for a week. Its whole value is that it can go RED on a silent miss, so the
BLIND and UNREADABLE cases below are the tests that matter — a canary that only
ever passes guards nothing.

The audit command is stubbed rather than run: these must not depend on OSV
being reachable, and stubbing is what lets a blind surface be simulated at all.
"""

import json
from pathlib import Path

import pytest
import yaml

from scripts.ci.audit_canary import (
    CANARY_PACKAGE,
    CANARY_REQUIREMENT,
    Verdict,
    audit_command,
    main,
    run_canary,
    verdict_for,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"

_SEEING_REPORT = json.dumps(
    {
        "dependencies": [
            {
                "name": "flask",
                "version": "0.12.2",
                "vulns": [
                    {
                        "id": "PYSEC-2018-66",
                        "fix_versions": ["0.12.3"],
                        "aliases": ["CVE-2018-1000656"],
                        "description": "DoS via JSON",
                    }
                ],
            }
        ],
        "fixes": [],
    }
)

_BLIND_REPORT = '{"dependencies": [{"name": "flask", "version": "0.12.2", "vulns": []}], "fixes": []}'


def _stub(tmp_path: Path, *, stdout: str, name: str = "stub.py") -> list[str]:
    """A stand-in audit command printing *stdout* and exiting like pip-audit."""
    script = tmp_path / name
    script.write_text(
        "import sys\n"
        f"sys.stdout.write({stdout!r})\n"
        # pip-audit exits non-zero when it finds something; the verdict must be
        # read from the report, never from this code.
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    return ["python3", str(script)]


class TestVerdict:
    def test_advisory_reported_is_seeing(self) -> None:
        assert verdict_for(_SEEING_REPORT) is Verdict.SEEING

    def test_no_advisory_on_a_known_vulnerable_pin_is_blind(self) -> None:
        assert verdict_for(_BLIND_REPORT) is Verdict.BLIND

    def test_absent_package_is_blind(self) -> None:
        assert verdict_for('{"dependencies": [], "fixes": []}') is Verdict.BLIND

    @pytest.mark.parametrize(
        "report",
        [
            "",
            "ERROR:pip_audit._cli: something broke",
            '{"no_dependencies_key": true}',
            '{"dependencies": "not-a-list"}',
            '{"dependencies": ["not-an-object"]}',
        ],
    )
    def test_unparseable_output_is_unreadable_never_blind(self, report: str) -> None:
        # UNREADABLE and BLIND are both failures but different diagnoses; a
        # crash must never be reported as "the surface saw nothing".
        assert verdict_for(report) is Verdict.UNREADABLE

    def test_package_match_is_case_insensitive(self) -> None:
        assert verdict_for('{"dependencies": [{"name": "Flask", "vulns": [{"id": "x"}]}]}') is Verdict.SEEING


class TestRunCanary:
    def test_healthy_surface_sees_the_pin(self, tmp_path: Path) -> None:
        verdict, _ = run_canary(command=_stub(tmp_path, stdout=_SEEING_REPORT))
        assert verdict is Verdict.SEEING

    def test_blind_surface_is_caught(self, tmp_path: Path) -> None:
        verdict, detail = run_canary(command=_stub(tmp_path, stdout=_BLIND_REPORT))
        assert verdict is Verdict.BLIND
        assert "stdout:" in detail

    def test_unrunnable_command_is_unreadable(self, tmp_path: Path) -> None:
        verdict, detail = run_canary(command=[str(tmp_path / "does-not-exist")])
        assert verdict is Verdict.UNREADABLE
        assert "could not be run" in detail

    def test_the_pin_reaches_the_audit_command(self, tmp_path: Path) -> None:
        # The requirements file the canary writes is what the tool audits; if it
        # were empty or missing the pin, a BLIND result would be self-inflicted.
        echo = tmp_path / "echo.py"
        echo.write_text(
            "import pathlib, sys\nsys.stdout.write(pathlib.Path(sys.argv[1]).read_text())\n",
            encoding="utf-8",
        )
        verdict, detail = run_canary(command=["python3", str(echo)])
        assert verdict is Verdict.UNREADABLE
        assert CANARY_REQUIREMENT in detail

    def test_default_command_targets_the_same_tool_and_backend_as_the_gate(self, tmp_path: Path) -> None:
        argv = audit_command(tmp_path / "requirements.canary.txt")
        assert argv[:2] == ["uvx", "pip-audit"]
        assert "--vulnerability-service" in argv
        assert argv[argv.index("--vulnerability-service") + 1] == "osv"


class TestMainExitCode:
    def test_seeing_exits_zero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("scripts.ci.audit_canary._AUDIT_COMMAND", tuple(_stub(tmp_path, stdout=_SEEING_REPORT)))
        assert main([]) == 0

    def test_blind_exits_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("scripts.ci.audit_canary._AUDIT_COMMAND", tuple(_stub(tmp_path, stdout=_BLIND_REPORT)))
        assert main([]) == 1
        assert "::error::audit canary BLIND" in capsys.readouterr().err

    def test_unreadable_exits_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("scripts.ci.audit_canary._AUDIT_COMMAND", tuple(_stub(tmp_path, stdout="not json")))
        assert main([]) == 1
        assert "::error::audit canary UNREADABLE" in capsys.readouterr().err


class TestCiWiring:
    @staticmethod
    def _audit_steps() -> list[dict[str, object]]:
        jobs = yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"]
        return [step for step in jobs["uv-audit"]["steps"] if isinstance(step, dict)]

    def test_canary_runs_in_the_audit_job_before_the_gate(self) -> None:
        runs = [str(step.get("run", "")) for step in self._audit_steps()]
        canary = next(i for i, run in enumerate(runs) if "scripts/ci/audit_canary.py" in run)
        gate = next(i for i, run in enumerate(runs) if "pip-audit --strict" in run)
        assert canary < gate, "the canary must prove the surface can see BEFORE the gate's green is trusted"

    def test_canary_needs_no_uv_sync(self) -> None:
        # The uv-audit job deliberately installs nothing; a `uv run` here would
        # add a full sync to every PR.
        canary = next(step for step in self._audit_steps() if "audit_canary.py" in str(step.get("run", "")))
        assert "uv run" not in str(canary["run"])

    def test_findings_are_annotated_with_reachability(self) -> None:
        annotate = next(step for step in self._audit_steps() if "t3 tool dependency-audit" in str(step.get("run", "")))
        assert "failure()" in str(annotate["if"]), "the assessment step must run when the gate finds something"

    def test_the_gate_itself_is_unchanged(self) -> None:
        # Behaviour preservation: the canary is additive, it never relaxes the gate.
        gate = next(step for step in self._audit_steps() if "pip-audit --strict" in str(step.get("run", "")))
        run = str(gate["run"])
        assert "--vulnerability-service osv" in run
        assert "--disable-pip" in run
        assert "$IGNORE_FLAGS" in run


def test_the_canary_pin_names_a_package_this_project_does_not_depend_on() -> None:
    # A canary drawn from our own dependency tree could be "fixed" by a routine
    # bump, silently retiring the guard.
    pyproject = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'"{CANARY_PACKAGE}' not in pyproject
