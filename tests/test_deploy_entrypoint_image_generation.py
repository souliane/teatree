# test-path: cross-cutting — drives deploy/entrypoint.sh as a real bash program (no src mirror).
"""An image generation's init only migrates, sets up and seeds; its watchdog is the one baked in the image."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[1] / "deploy" / "entrypoint.sh"
BASH = shutil.which("bash") or ""
SHA = "c" * 40


_STUBBED_FUNCTIONS = (
    "init_preflight",
    "ensure_clone",
    "assert_core_source",
    "ensure_uv_constraints",
    "require_install_headroom",
    "prepare_agent_homes",
    "refuse_on_constraint_skew",
    "seed_setting",
)


def _init_arm() -> str:
    lines = ENTRYPOINT.read_text(encoding="utf-8").splitlines()
    start = lines.index("init)")
    end = next(index for index in range(start, len(lines)) if lines[index].strip() == ";;")
    return "\n".join(lines[start + 1 : end])


def _logging_tool(path: Path, name: str) -> None:
    path.write_text(f'#!/bin/bash\nprintf "{name} %s\\n" "$*" >>"$CALLS"\n', encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run_init(tmp_path: Path, generation: str) -> list[str]:
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for tool in ("uv", "prek", "t3", "git"):
        _logging_tool(stubs / tool, tool)
    python_root = tmp_path / "python"
    (python_root / "cpython-3.13").mkdir(parents=True)
    calls = tmp_path / "calls"
    calls.touch()
    functions = "\n".join(f'{name}() {{ printf "fn {name}\\n" >>"$CALLS"; }}' for name in _STUBBED_FUNCTIONS)
    script = "\n".join(
        [
            "set -euo pipefail",
            functions,
            "network_up() { return 0; }",
            f'GENERATION="{generation}"',
            f'CLONE_DIR="{tmp_path}"',
            'CONSTRAINTS_FILE="$CLONE_DIR/uv-constraints.txt"',
            'HOST_ROOT=""',
            _init_arm(),
        ]
    )
    env = {
        "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
        "CALLS": str(calls),
        "UV_PYTHON_INSTALL_DIR": str(python_root),
        "HOME": str(tmp_path),
    }
    (tmp_path / "locked-version.sh").write_text("echo 0.0.1\n", encoding="utf-8")
    (tmp_path / "entrypoint-init.sh").write_text(script, encoding="utf-8")
    subprocess.run(
        [BASH, str(tmp_path / "entrypoint-init.sh")], check=True, env=env, capture_output=True, text=True, cwd=tmp_path
    )
    return calls.read_text(encoding="utf-8").splitlines()


class TestInitInAnImageGeneration:
    def test_nothing_is_fetched_installed_or_hooked(self, tmp_path: Path) -> None:
        calls = _run_init(tmp_path, SHA)

        assert "fn ensure_clone" not in calls
        assert "fn ensure_uv_constraints" not in calls
        assert not [call for call in calls if call.split(" ", 1)[0] in {"uv", "prek", "git"}]

    def test_it_still_sets_up_migrates_and_reopens_admission(self, tmp_path: Path) -> None:
        calls = _run_init(tmp_path, SHA)

        assert "fn prepare_agent_homes" in calls
        assert "t3 teatree db migrate" in calls
        assert "t3 teatree config_setting set worker_quiescing false" in calls


@pytest.mark.parametrize("generation", [SHA, ""])
def test_init_migrates_before_setup_reads_the_config_table(tmp_path: Path, generation: str) -> None:
    calls = _run_init(tmp_path, generation)

    assert calls.index("t3 teatree db migrate") < calls.index("fn prepare_agent_homes")


class TestInitOfALegacyStackIsUnchanged:
    def test_the_clone_is_refreshed_and_the_runtime_installed(self, tmp_path: Path) -> None:
        calls = _run_init(tmp_path, "")

        assert "fn ensure_clone" in calls
        assert "fn ensure_uv_constraints" in calls
        assert any(call.startswith("uv tool install --editable") for call in calls)
        assert "prek install -f" in calls


def _run_watchdog(tmp_path: Path, generation: str) -> str:
    baked, checkout = tmp_path / "baked", tmp_path / "checkout"
    for root, label in ((baked, "baked"), (checkout, "checkout")):
        (root / "deploy").mkdir(parents=True)
        (root / "deploy" / "watchdog.sh").write_text(f'echo "{label} $*"\n', encoding="utf-8")
    env = {
        "PATH": os.environ["PATH"],
        "TEATREE_ROLE": "watchdog",
        "TEATREE_GENERATION": generation,
        "TEATREE_CLONE_DIR": str(baked),
        "TEATREE_DEPLOY_CHECKOUT": str(checkout),
    }
    return subprocess.run([BASH, str(ENTRYPOINT)], check=True, env=env, capture_output=True, text=True).stdout


@pytest.mark.parametrize(("generation", "expected"), [(SHA, "baked --loop"), ("", "checkout --loop")])
def test_the_watchdog_runs_from_the_image_of_a_generation_and_the_checkout_otherwise(
    tmp_path: Path, generation: str, expected: str
) -> None:
    assert _run_watchdog(tmp_path, generation).strip() == expected
