"""The hook runtime is resolved without going through the ``t3`` entry point (#3964).

Host ``t3`` is a shell shim that dispatches into the worker container: there is no
interpreter beside it to discover, and the CLI it reaches is the container's rather
than this host's. So ``run-hook.sh`` names the hook venv directly. Left resolving
through the entry point, a shimmed host would fall through to a bare ``python3``
with no Django — and every ORM-backed handler (the hand-off drain, the away-mode
question recorder, the standing-goal and unknown-repo-push gates) silently no-ops
while the session looks healthy.

The entry-point probe is kept as a demoted fallback, because it is the only route
to a non-uv host install (pipx, a hand-rolled venv). Both halves are pinned here:
the named venv outranks it, and it still resolves when no named venv exists.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUN_HOOK = _REPO_ROOT / "hooks" / "scripts" / "run-hook.sh"

_SHELL_ESSENTIALS = ("bash", "sh", "env", "readlink")


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _fake_interpreter(path: Path, name: str, log: Path, *, has_django: bool) -> None:
    """A stand-in interpreter that records being CHOSEN, then delegates for real.

    Only a non-``-c`` invocation is logged: ``-c`` is the probe, and every
    candidate gets probed, so logging those would say nothing about which one the
    shim actually exec'd.
    """
    django_branch = ":" if has_django else "exit 1"
    _write_executable(
        path,
        f"""#!/usr/bin/env bash
case "$*" in
*"import django"*) {django_branch} ;;
esac
case "${{1:-}}" in
-c) ;;
*) printf '%s\\n' {name!r} >>{str(log)!r} ;;
esac
exec {sys.executable!r} "$@"
""",
    )


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    """A PATH directory carrying the shell essentials and nothing python-shaped."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for tool in _SHELL_ESSENTIALS:
        source = shutil.which(tool)
        if source:
            (fake_bin / tool).symlink_to(source)
    return fake_bin


@pytest.fixture
def hook_script(tmp_path: Path) -> Path:
    script = tmp_path / "hook.py"
    script.write_text("print('ran')\n", encoding="utf-8")
    return script


def _run(hook_script: Path, bin_dir: Path, **env_extra: str) -> subprocess.CompletedProcess[str]:
    env = {"PATH": str(bin_dir), "HOME": str(bin_dir.parent / "nonexistent-home")}
    env.update(env_extra)
    return subprocess.run(
        [shutil.which("bash") or "bash", str(_RUN_HOOK), str(hook_script)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=60,
    )


def _chosen(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").split() if log.exists() else []


class TestNamedHookVenvIsUsedWithoutTheEntryPoint:
    def test_a_shimmed_t3_still_reaches_the_django_capable_hook_venv(
        self, tmp_path: Path, bin_dir: Path, hook_script: Path
    ) -> None:
        log = tmp_path / "chosen.log"
        uv_tools = tmp_path / "uvtools"
        _fake_interpreter(uv_tools / "teatree" / "bin" / "python", "hook-venv", log, has_django=True)
        # The host `t3` a shim leaves behind: no interpreter anywhere beside it.
        _write_executable(bin_dir / "t3", '#!/usr/bin/env bash\nexec docker compose exec worker t3 "$@"\n')

        proc = _run(hook_script, bin_dir, UV_TOOL_DIR=str(uv_tools))

        assert proc.returncode == 0
        assert _chosen(log) == ["hook-venv"]

    def test_the_named_venv_outranks_an_interpreter_beside_the_entry_point(
        self, tmp_path: Path, bin_dir: Path, hook_script: Path
    ) -> None:
        log = tmp_path / "chosen.log"
        uv_tools = tmp_path / "uvtools"
        _fake_interpreter(uv_tools / "teatree" / "bin" / "python", "hook-venv", log, has_django=True)
        _write_executable(bin_dir / "t3", "#!/usr/bin/env bash\nexit 0\n")
        _fake_interpreter(bin_dir / "python", "beside-t3", log, has_django=True)

        proc = _run(hook_script, bin_dir, UV_TOOL_DIR=str(uv_tools))

        assert proc.returncode == 0
        assert _chosen(log) == ["hook-venv"]


class TestEntryPointFallbackIsPreserved:
    """A non-uv host install is reachable only through the demoted probe."""

    def test_a_venv_beside_the_entry_point_still_resolves(
        self, tmp_path: Path, bin_dir: Path, hook_script: Path
    ) -> None:
        log = tmp_path / "chosen.log"
        venv_bin = tmp_path / "pipx-venv" / "bin"
        _write_executable(venv_bin / "t3", "#!/usr/bin/env bash\nexit 0\n")
        _fake_interpreter(venv_bin / "python", "beside-t3", log, has_django=True)
        (bin_dir / "t3").symlink_to(venv_bin / "t3")

        proc = _run(hook_script, bin_dir, UV_TOOL_DIR=str(tmp_path / "empty-uvtools"))

        assert proc.returncode == 0
        assert _chosen(log) == ["beside-t3"]


class TestUvToolDirIsHonoured:
    def test_the_default_uv_layout_is_still_found_without_the_env_var(
        self, tmp_path: Path, bin_dir: Path, hook_script: Path
    ) -> None:
        log = tmp_path / "chosen.log"
        home = tmp_path / "home"
        _fake_interpreter(
            home / ".local" / "share" / "uv" / "tools" / "teatree" / "bin" / "python",
            "hook-venv",
            log,
            has_django=True,
        )

        env = {"PATH": str(bin_dir), "HOME": str(home)}
        proc = subprocess.run(
            [shutil.which("bash") or "bash", str(_RUN_HOOK), str(hook_script)],
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=60,
        )

        assert proc.returncode == 0
        assert _chosen(log) == ["hook-venv"]

    def test_an_unset_home_names_no_candidate_rather_than_a_root_path(
        self, tmp_path: Path, bin_dir: Path, hook_script: Path
    ) -> None:
        """``/.local/share/uv/tools`` is nobody's venv — probing it is noise."""
        log = tmp_path / "chosen.log"
        _fake_interpreter(bin_dir / "python3", "path-python", log, has_django=True)

        proc = subprocess.run(
            [shutil.which("bash") or "bash", str(_RUN_HOOK), str(hook_script)],
            capture_output=True,
            text=True,
            env={"PATH": str(bin_dir)},
            check=False,
            timeout=60,
        )

        assert proc.returncode == 0
        assert _chosen(log) == ["path-python"]
        assert os.sep in str(bin_dir)
