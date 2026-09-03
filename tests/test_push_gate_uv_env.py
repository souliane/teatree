# test-path: cross-cutting — asserts over dev/push-gate.sh; no src/teatree/ mirror.
"""The push gate must not reconcile the environment it is importing from.

``dev/push-gate.sh`` runs from a repo that may be a uv workspace MEMBER (a vendored
``vendor/teatree`` inside a fork). A bare ``uv run`` there SYNCS the environment it
resolves, and for a member that is the workspace ROOT's shared ``.venv`` — reconciled
to the member's dependency set while this very gate imports from it. The failure is a
race, not a code defect: the gate dies on ``ImportError``/``ModuleNotFoundError``
naming packages no diff went near, the count varies run to run, and a re-run of
identical code passes.

``uv_project_run_prefix`` (``scripts/hooks/lib/resolve-uv.sh``) exists to close exactly
this, and redirects every member to a hook-owned ``UV_PROJECT_ENVIRONMENT``. These
tests drive the real script against a ``uv`` stub that records the environment each
invocation was pointed at — the only thing that separates "shared" from "ours".
"""

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: What ``resolve-uv.sh`` redirects a workspace member to.
_HOOK_ENV = ".venv-hook"

#: Recorded when an invocation carried no ``UV_PROJECT_ENVIRONMENT`` — it took whatever
#: environment uv resolved on its own, which for a member is the shared one.
_NO_REDIRECT = "-"

_UV_STUB = f"""#!/usr/bin/env bash
set -euo pipefail
if [ "${{1:-}}" = "--version" ]; then echo "uv 0.0.0-test"; exit 0; fi
printf '%s\\n' "${{UV_PROJECT_ENVIRONMENT:-{_NO_REDIRECT}}}" >>"${{UV_STUB_ENVS}}"
"""

_COPIED = ("dev/push-gate.sh", "dev/lib/xdist-workers.sh", "scripts/hooks/lib/resolve-uv.sh")


def _lane_repo(tmp_path: Path, *, vendored: bool) -> Path:
    """Return the copied lane script; ``vendored`` puts it in a workspace MEMBER."""
    root = tmp_path / "root"
    lane_repo = root / "vendor" / "teatree" if vendored else root
    for name in _COPIED:
        target = lane_repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_REPO_ROOT / name, target)
    root.joinpath("pyproject.toml").write_text(
        '[project]\nname = "root"\n[tool.uv.workspace]\nmembers = ["vendor/teatree"]\n'
        if vendored
        else '[project]\nname = "standalone"\n',
        encoding="utf-8",
    )
    if vendored:
        lane_repo.joinpath("pyproject.toml").write_text('[project]\nname = "member"\n', encoding="utf-8")
    return lane_repo / "dev" / "push-gate.sh"


def _install_uv_stub(tmp_path: Path) -> None:
    stub = tmp_path / "bin" / "uv"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text(_UV_STUB, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run(tmp_path: Path, script: Path) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    envs_log = tmp_path / "envs.log"
    completed = subprocess.run(
        [str(script)],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
            "HOME": str(home),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "PYENV_ROOT": str(tmp_path / "pyenv"),
            "ASDF_DATA_DIR": str(tmp_path / "asdf"),
            "T3_CGROUP_MEMORY_MAX_V2": str(tmp_path / "absent-v2"),
            "T3_CGROUP_MEMORY_MAX_V1": str(tmp_path / "absent-v1"),
            "UV_STUB_ENVS": str(envs_log),
        },
    )
    recorded = envs_log.read_text(encoding="utf-8").split() if envs_log.exists() else []
    return completed, recorded


@pytest.mark.integration
class TestAVendoredLaneNeverSyncsTheSharedEnvironment:
    def test_every_step_runs_against_the_hooks_own_environment(self, tmp_path: Path) -> None:
        _install_uv_stub(tmp_path)

        completed, recorded = _run(tmp_path, _lane_repo(tmp_path, vendored=True))

        assert completed.returncode == 0, f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
        assert recorded == [_HOOK_ENV] * 3, (
            "a step ran against the environment uv resolves on its own — for a workspace member that is "
            f"the ROOT's shared `.venv`, reconciled mid-run under the gate importing from it: {recorded}"
        )


@pytest.mark.integration
class TestAStandaloneCheckoutIsLeftAlone:
    """The control: the probe can observe a non-redirected run, and does here."""

    def test_a_repo_that_owns_its_environment_keeps_being_managed_in_place(self, tmp_path: Path) -> None:
        _install_uv_stub(tmp_path)

        completed, recorded = _run(tmp_path, _lane_repo(tmp_path, vendored=False))

        assert completed.returncode == 0, f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
        assert recorded == [_NO_REDIRECT] * 3, f"a standalone checkout must not be redirected: {recorded}"


@pytest.mark.integration
class TestAnUnresolvableUvFailsClosed:
    def test_no_uv_at_all_refuses_the_push_rather_than_reporting_a_pass(self, tmp_path: Path) -> None:
        completed, recorded = _run(tmp_path, _lane_repo(tmp_path, vendored=True))

        assert completed.returncode != 0, "a gate that cannot run its own checks must never report a pass"
        assert recorded == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
