import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.ci.cooldown_escapes import main

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "cooldown_escapes.py"
_CI = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_DEPENDABOT = _REPO_ROOT / ".github" / "dependabot.yml"
_HOOK_CONFIGS = (".pre-commit-config.yaml", "src/teatree/templates/overlay/.pre-commit-config.yaml.tmpl")
_UV_HOOK = re.compile(
    r"repo: https://github\.com/astral-sh/uv-pre-commit\n\s+rev: (?P<sha>[0-9a-f]{40})\s+# (?P<version>\d+\.\d+\.\d+)\n"
)
_FIRST_UV_PARSING_A_SPAN = (0, 9, 17)

_NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
_MCP_JSON_URL = "https://pypi.org/pypi/mcp/2.3.0/json"
_MCP_WHEEL = "mcp-2.3.0-py3-none-any.whl"
_MCP_SDIST = "mcp-2.3.0.tar.gz"
_MCP_FILES = ((_MCP_WHEEL, "2026-10-02T22:06:52.119Z"), (_MCP_SDIST, "2026-10-02T22:06:56.091Z"))
_ROUNDED_UP = "2026-10-02T22:06:57Z"

type FetchJson = Callable[[str], Mapping[str, Any]]


def _hook_pin(config: str) -> tuple[str, str]:
    match = _UV_HOOK.search((_REPO_ROOT / config).read_text(encoding="utf-8"))
    assert match is not None, f"{config} must pin astral-sh/uv-pre-commit to a commit with its version label."
    return match["sha"], match["version"]


def _audit_step(fragment: str) -> dict[str, Any]:
    steps = yaml.safe_load(_CI.read_text(encoding="utf-8"))["jobs"]["uv-audit"]["steps"]
    step = next((step for step in steps if fragment in step.get("run", "")), None)
    assert step is not None, f"The uv-audit job has no step running {fragment!r}."
    return step


def _pyproject(tmp_path: Path, escapes: str = "") -> Path:
    path = tmp_path / "pyproject.toml"
    body = '[project]\nname = "demo"\n\n[tool.uv]\nexclude-newer = "7 days"\n'
    path.write_text(body + (f"exclude-newer-package = {{ {escapes} }}\n" if escapes else ""), encoding="utf-8")
    return path


def _lock(tmp_path: Path, files: tuple[str, ...] = (_MCP_WHEEL, _MCP_SDIST)) -> Path:
    def url(filename: str) -> str:
        return f'{{ url = "https://files.pythonhosted.org/packages/00/{filename}" }}'

    sdist = next((f"sdist = {url(name)}\n" for name in files if not name.endswith(".whl")), "")
    wheels = ", ".join(url(name) for name in files if name.endswith(".whl"))
    path = tmp_path / "uv.lock"
    path.write_text(
        'version = 1\nrequires-python = ">=3.13"\n\n[[package]]\nname = "mcp"\nversion = "2.3.0"\n'
        f'source = {{ registry = "https://pypi.org/simple" }}\n{sdist}wheels = [{wheels}]\n',
        encoding="utf-8",
    )
    return path


def _pypi(files: tuple[tuple[str, str], ...] = _MCP_FILES) -> FetchJson:
    payload = json.dumps({"urls": [{"filename": name, "upload_time_iso_8601": at} for name, at in files]})

    def fetch(url: str) -> Mapping[str, Any]:
        assert url == _MCP_JSON_URL
        return json.loads(payload)

    return fetch


def _unreachable(url: str) -> Mapping[str, Any]:
    raise OSError(url)


def _run(
    tmp_path: Path,
    escapes: str,
    *flags: str,
    now: datetime = _NOW,
    fetch_json: FetchJson = _unreachable,
    lock: Path | None = None,
) -> int:
    argv = ["--pyproject", str(_pyproject(tmp_path, escapes)), "--lock", str(lock or _lock(tmp_path)), *flags]
    return main(argv, now=now, fetch_json=fetch_json)


class TestTheRepoHoldsNewReleases:
    def test_pyproject_waits_seven_days_before_locking_a_new_release(self) -> None:
        uv = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["uv"]
        assert uv["exclude-newer"] == "7 days"

    def test_the_lock_was_resolved_under_that_window(self) -> None:
        options = tomllib.loads((_REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))["options"]
        assert options["exclude-newer-span"] == "P7D"

    def test_every_uv_lock_hook_runs_one_uv_that_reads_the_window(self) -> None:
        pins = {_hook_pin(config) for config in _HOOK_CONFIGS}
        assert len(pins) == 1, f"The repo and the overlay template must pin the same uv-pre-commit rev: {pins}"
        ((_, version),) = pins
        assert tuple(int(part) for part in version.split(".")) >= _FIRST_UV_PARSING_A_SPAN, (
            f'uv {version} cannot parse `exclude-newer = "7 days"`: it drops [tool.uv] and rewrites uv.lock.'
        )

    def test_dependabot_waits_as_long_as_uv(self) -> None:
        days = int(
            tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["uv"][
                "exclude-newer"
            ].split()[0]
        )
        updates = yaml.safe_load(_DEPENDABOT.read_text(encoding="utf-8"))["updates"]
        assert {entry["cooldown"]["default-days"] for entry in updates} == {days}

    def test_the_committed_escapes_are_well_formed(self) -> None:
        argv = ["--pyproject", str(_REPO_ROOT / "pyproject.toml"), "--lock", str(_REPO_ROOT / "uv.lock")]
        assert main(argv, now=datetime.now(UTC), fetch_json=_unreachable) == 0


class TestTheAuditRunsTheEscapeCheck:
    def test_every_run_checks_the_escapes_and_only_the_schedule_fails_on_expiry(self) -> None:
        step = _audit_step("scripts/ci/cooldown_escapes.py")
        assert "--verify-lock" in step["run"]
        assert "--fail-on-expired" not in step["run"]
        assert step["env"]["FAIL_ON_EXPIRED"] == "${{ github.event_name == 'schedule' && '--fail-on-expired' || '' }}"
        assert step["if"] == "${{ !cancelled() }}", "The check must also run when pip-audit reds."

    def test_a_red_audit_points_at_the_escape_routine(self) -> None:
        step = _audit_step("exclude-newer-package")
        assert step["if"] == "failure() && steps.audit.outcome == 'failure'"
        assert "BLUEPRINT §15" in step["run"]


class TestEscapeEntries:
    def test_a_fresh_escape_passes_the_expiry_check(self, tmp_path: Path) -> None:
        assert _run(tmp_path, f'mcp = "{_ROUNDED_UP}"', "--fail-on-expired") == 0

    def test_an_escape_the_window_has_caught_up_with_fails_the_expiry_check(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        caught_up = datetime(2026, 10, 9, 22, 6, 57, tzinfo=UTC)
        assert _run(tmp_path, f'mcp = "{_ROUNDED_UP}"', "--fail-on-expired", now=caught_up) == 1
        assert "::error::" in (err := capsys.readouterr().err)
        assert "mcp" in err

    def test_an_escape_one_second_inside_its_window_is_still_live(self, tmp_path: Path) -> None:
        inside = datetime(2026, 10, 9, 22, 6, 56, tzinfo=UTC)
        assert _run(tmp_path, f'mcp = "{_ROUNDED_UP}"', "--fail-on-expired", now=inside) == 0

    def test_an_expired_escape_only_warns_without_the_flag(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert _run(tmp_path, 'mcp = "2026-09-01T00:00:00Z"') == 0
        assert "::warning::" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("value", "reason"),
        [
            ("false", "never expires"),
            ('"2026-10-02T22:06:56.091Z"', "whole second"),
            ('"2026-10-02"', "whole second"),
            ('"2026-02-30T22:06:56Z"', "invalid UTC timestamp"),
        ],
    )
    def test_a_malformed_escape_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], value: str, reason: str
    ) -> None:
        assert _run(tmp_path, f"mcp = {value}") == 1
        assert reason in capsys.readouterr().err

    def test_a_non_table_escape_value_is_refused(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[tool.uv]\nexclude-newer = "7 days"\nexclude-newer-package = "mcp"\n', encoding="utf-8")
        assert main(["--pyproject", str(pyproject), "--lock", str(_lock(tmp_path))], now=_NOW) == 1
        assert "must be a table" in capsys.readouterr().err

    def test_runs_on_stdlib_alone(self, tmp_path: Path) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-S",
                str(_SCRIPT),
                "--pyproject",
                str(_pyproject(tmp_path, f'mcp = "{_ROUNDED_UP}"')),
                "--lock",
                str(_lock(tmp_path)),
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=tmp_path,
        )
        assert completed.returncode == 0, completed.stderr


class TestVerifyLock:
    def test_passes_with_the_newest_upload_rounded_up_and_every_file_locked(self, tmp_path: Path) -> None:
        assert _run(tmp_path, f'mcp = "{_ROUNDED_UP}"', "--verify-lock", fetch_json=_pypi()) == 0

    def test_fails_on_a_cutoff_equal_to_a_file_upload_time(self, tmp_path: Path) -> None:
        assert _run(tmp_path, 'mcp = "2026-10-02T22:06:56.091Z"', "--verify-lock", fetch_json=_pypi()) == 1

    def test_fails_on_a_whole_second_cutoff_equal_to_the_newest_upload(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        on_the_second = ((_MCP_WHEEL, "2026-10-02T22:06:52.119Z"), (_MCP_SDIST, "2026-10-02T22:06:57.000Z"))
        assert _run(tmp_path, f'mcp = "{_ROUNDED_UP}"', "--verify-lock", fetch_json=_pypi(on_the_second)) == 1
        assert "2026-10-02T22:06:58Z" in capsys.readouterr().err

    def test_fails_on_a_cutoff_later_than_the_rounded_up_upload(self, tmp_path: Path) -> None:
        assert _run(tmp_path, 'mcp = "2026-10-05T00:00:00Z"', "--verify-lock", fetch_json=_pypi()) == 1

    def test_fails_when_the_lock_lacks_a_file_pypi_lists(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wheel_only = _lock(tmp_path, (_MCP_WHEEL,))
        assert _run(tmp_path, f'mcp = "{_ROUNDED_UP}"', "--verify-lock", fetch_json=_pypi(), lock=wheel_only) == 1
        assert _MCP_SDIST in capsys.readouterr().err

    def test_a_wheel_the_locked_python_cannot_run_is_not_required(self, tmp_path: Path) -> None:
        older_python = ("mcp-2.3.0-cp312-cp312-manylinux_2_17_x86_64.whl", "2026-10-02T22:06:50.000Z")
        files = (*_MCP_FILES, older_python)
        assert _run(tmp_path, f'mcp = "{_ROUNDED_UP}"', "--verify-lock", fetch_json=_pypi(files)) == 0

    def test_fails_when_the_escaped_package_is_not_locked(self, tmp_path: Path) -> None:
        assert _run(tmp_path, f'mcp-types = "{_ROUNDED_UP}"', "--verify-lock", fetch_json=_pypi()) == 1

    def test_an_unreadable_pypi_fails_loud(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert _run(tmp_path, f'mcp = "{_ROUNDED_UP}"', "--verify-lock") == 2
        assert "::error::" in capsys.readouterr().err
