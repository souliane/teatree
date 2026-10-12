"""A copied ``dev/push-gate.sh`` lane driven against a ``uv`` stub that logs every stage it is asked to run."""

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: Recorded when an invocation carried no ``UV_PROJECT_ENVIRONMENT`` — it took whatever
#: environment uv resolved on its own, which for a member is the shared one.
NO_REDIRECT = "-"

UV_STUB = f"""#!/usr/bin/env bash
set -euo pipefail
if [ "${{1:-}}" = "--version" ]; then echo "uv 0.0.0-test"; exit 0; fi
printf '%s\\n' "${{UV_PROJECT_ENVIRONMENT:-{NO_REDIRECT}}}" >>"${{UV_STUB_ENVS}}"
printf '%s\\n' "${{DJANGO_SETTINGS_MODULE:-{NO_REDIRECT}}}" >>"${{UV_STUB_DJANGO_SETTINGS}}"
"""

COPIED = (
    "dev/push-gate.sh",
    "dev/lib/gate-record.sh",
    "dev/lib/gate-lock.sh",
    "dev/lib/xdist-workers.sh",
    "scripts/hooks/lib/resolve-uv.sh",
    "src/teatree/utils/push_gate_lock.py",
)


def lane_repo(tmp_path: Path, *, vendored: bool) -> Path:
    """Return the copied lane script; ``vendored`` puts it in a workspace MEMBER."""
    root = tmp_path / "root"
    lane = root / "vendor" / "teatree" if vendored else root
    for name in COPIED:
        target = lane / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_REPO_ROOT / name, target)
    root.joinpath("pyproject.toml").write_text(
        '[project]\nname = "root"\n[tool.uv.workspace]\nmembers = ["vendor/teatree"]\n'
        if vendored
        else '[project]\nname = "standalone"\n',
        encoding="utf-8",
    )
    if vendored:
        lane.joinpath("pyproject.toml").write_text('[project]\nname = "member"\n', encoding="utf-8")
    return lane / "dev" / "push-gate.sh"


def gnu_timeout() -> Path:
    for name in ("timeout", "gtimeout"):
        if found := shutil.which(name):
            return Path(found)
    pytest.skip("push-gate.sh needs GNU timeout(1) as `timeout` or `gtimeout`; neither is on PATH")


def link_gnu_timeout(tmp_path: Path) -> None:
    timeout = gnu_timeout()
    link = tmp_path / "bin" / timeout.name
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(timeout)


def install_uv_stub(tmp_path: Path) -> None:
    stub = tmp_path / "bin" / "uv"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text(UV_STUB, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def run_lane(
    tmp_path: Path, script: Path, *, extra_env: dict[str, str] | None = None
) -> tuple[subprocess.CompletedProcess[str], list[str], list[str]]:
    link_gnu_timeout(tmp_path)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    envs_log = tmp_path / "envs.log"
    django_settings_log = tmp_path / "django_settings.log"
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
            # Never the machine-wide lock: a test run must not queue behind, or overwrite, a real gate.
            "T3_PUSH_GATE_LOCK": str(tmp_path / "push-gate.lock"),
            "UV_STUB_ENVS": str(envs_log),
            "UV_STUB_DJANGO_SETTINGS": str(django_settings_log),
            **(extra_env or {}),
        },
    )
    recorded = envs_log.read_text(encoding="utf-8").split() if envs_log.exists() else []
    django_settings = django_settings_log.read_text(encoding="utf-8").split() if django_settings_log.exists() else []
    return completed, recorded, django_settings
