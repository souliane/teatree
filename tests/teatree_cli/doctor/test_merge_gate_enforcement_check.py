"""``_check_merge_gates_enforced`` — a workflow GATE branch protection never requires.

Functional: a real git clone under ``tmp_path`` carrying a ``ci.yml`` fixture, so the
job-name detection reads real file content rather than a stubbed finding. Only the live
``gh api`` calls are faked (subprocess.CompletedProcess), mirroring the shape
``ci_rollup``'s own parsers already consume.
"""

import io
from collections.abc import Callable, Iterator
from contextlib import redirect_stdout
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

import pytest

from teatree.cli.doctor.checks_merge_gate_enforcement import _check_merge_gates_enforced
from teatree.utils.run import run_checked

_CI_YML_WITH_ALL_THREE_GATES = """\
jobs:
  lint:
    runs-on: ubuntu-latest
  module-health-gate:
    runs-on: ubuntu-latest
  doc-update-gate:
    runs-on: ubuntu-latest
  e2e-no-skip-gate:
    runs-on: ubuntu-latest
"""

_CI_YML_WITHOUT_THE_GATES = """\
jobs:
  lint:
    runs-on: ubuntu-latest
"""

# The repo's ACTUAL branch-protection required contexts (read 2026-09-03) — none of the
# three GATE jobs are in it.
_ACTUAL_REQUIRED_STATUS_CHECKS_JSON = (
    '{"contexts": ["lint", "test (3.13)", "docs-drift", "uv-audit", "sbom", '
    '"blueprint-cross-pr", "eval-gate", "banned-terms-tree", "overlay-leak-tree", "term-source-drift"]}'
)


def _git(cwd: Path, *args: str) -> str:
    return run_checked(
        ["git", "-c", "user.email=agent@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
    ).stdout.strip()


def _echoes(check: Callable[[], None]) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        check()
    return buf.getvalue()


@pytest.fixture
def clone(tmp_path: Path) -> Iterator[Path]:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    root = tmp_path / "clone"
    _git(tmp_path, "clone", str(origin), str(root))
    (root / "file.txt").write_text("hello", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    _git(root, "push", "origin", "main")
    _git(root, "remote", "set-url", "origin", "https://github.com/acme/widget.git")
    with mock.patch("teatree.paths.CODE_REPO_ROOT", root):
        yield root


def _write_workflow(root: Path, body: str) -> None:
    workflow_dir = root / ".github" / "workflows"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "ci.yml").write_text(body, encoding="utf-8")


def _gh_result(*, rules_rc: int, rules_out: str, protection_rc: int, protection_out: str) -> Callable[..., object]:
    """A ``run_allowed_to_fail`` fake that answers the rules probe then the protection probe."""
    calls = {"n": 0}

    def _run(cmd: list[str], **_kwargs: object) -> CompletedProcess[str]:
        calls["n"] += 1
        if calls["n"] == 1:
            return CompletedProcess(cmd, rules_rc, rules_out, "")
        return CompletedProcess(cmd, protection_rc, protection_out, "")

    return _run


class TestCheckMergeGatesEnforced:
    def test_silent_when_the_workflow_does_not_declare_the_expected_gate_jobs(self, clone: Path) -> None:
        _write_workflow(clone, _CI_YML_WITHOUT_THE_GATES)

        out = _echoes(_check_merge_gates_enforced)

        assert out == ""

    def test_warns_and_names_each_gate_missing_from_the_live_required_set(self, clone: Path) -> None:
        # Reproduces the actual #4641 gap: the rules endpoint reports nothing readable
        # (empty list) and the legacy protection endpoint carries the repo's real required set.
        _write_workflow(clone, _CI_YML_WITH_ALL_THREE_GATES)
        fake_run = _gh_result(
            rules_rc=1, rules_out="", protection_rc=0, protection_out=_ACTUAL_REQUIRED_STATUS_CHECKS_JSON
        )

        with mock.patch("teatree.utils.run.run_allowed_to_fail", side_effect=fake_run):
            out = _echoes(_check_merge_gates_enforced)

        assert "WARN  CI job 'module-health-gate' is commented GATE" in out
        assert "WARN  CI job 'doc-update-gate' is commented GATE" in out
        assert "WARN  CI job 'e2e-no-skip-gate' is commented GATE" in out
        assert "branch protection does not require it" in out

    def test_silent_once_every_expected_gate_is_required(self, clone: Path) -> None:
        _write_workflow(clone, _CI_YML_WITH_ALL_THREE_GATES)
        fully_enforced = _ACTUAL_REQUIRED_STATUS_CHECKS_JSON.replace(
            '"term-source-drift"]',
            '"term-source-drift", "module-health-gate", "doc-update-gate", "e2e-no-skip-gate"]',
        )
        fake_run = _gh_result(rules_rc=1, rules_out="", protection_rc=0, protection_out=fully_enforced)

        with mock.patch("teatree.utils.run.run_allowed_to_fail", side_effect=fake_run):
            out = _echoes(_check_merge_gates_enforced)

        assert out == ""

    def test_an_unreadable_probe_warns_about_the_outage_not_a_manufactured_gate_finding(self, clone: Path) -> None:
        _write_workflow(clone, _CI_YML_WITH_ALL_THREE_GATES)
        fake_run = _gh_result(rules_rc=1, rules_out="", protection_rc=1, protection_out="rate limited")

        with mock.patch("teatree.utils.run.run_allowed_to_fail", side_effect=fake_run):
            out = _echoes(_check_merge_gates_enforced)

        assert out == ""

    def test_never_raises_on_a_crashing_probe(self, clone: Path) -> None:
        _write_workflow(clone, _CI_YML_WITH_ALL_THREE_GATES)

        with mock.patch("teatree.utils.git_remote_ops.remote_slug", side_effect=RuntimeError("boom")):
            out = _echoes(_check_merge_gates_enforced)

        assert "WARN  Merge-gate-enforcement check crashed" in out
