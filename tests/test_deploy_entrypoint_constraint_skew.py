# test-path: cross-cutting — drives deploy/entrypoint.sh's init refusal (no src mirror).
"""Init refuses a tool venv off its boot constraints before starting long-lived roles.

A worker whose installed dependencies drifted from the lock crash-looped 361 times on
``TypeError: Worker.__init__() got an unexpected keyword argument``, which names neither
the package nor the fix. The refusal runs the tool venv's own interpreter, so it judges
exactly what ``t3`` would import.
"""

import re
import shutil
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

_TEATREE = Path(__file__).resolve().parents[1]
ENTRYPOINT = _TEATREE / "deploy" / "entrypoint.sh"
_BASH = shutil.which("bash") or "/bin/bash"
_GUARD = "refuse_on_constraint_skew"


def _extract_shell_function(name: str) -> str:
    lines = ENTRYPOINT.read_text(encoding="utf-8").splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(f"{name}() {{")), None)
    assert start is not None, f"function {name!r} not found in {ENTRYPOINT}"
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start : end + 1])


def _run_guard(tmp_path: Path, constraints: str) -> subprocess.CompletedProcess[str]:
    tool_bin = tmp_path / "tools" / "teatree" / "bin"
    tool_bin.mkdir(parents=True)
    (tool_bin / "python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    (tool_bin / "python").chmod(0o755)
    (tool_bin / "t3").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (tool_bin / "t3").chmod(0o755)
    constraints_file = tmp_path / "uv-constraints.txt"
    constraints_file.write_text(constraints, encoding="utf-8")
    script = (
        f"CLONE_DIR={_TEATREE}\nCONSTRAINTS_FILE={constraints_file}\n"
        f"{_extract_shell_function(_GUARD)}\n{_GUARD} init\necho INIT_COMPLETE\n"
    )
    env = {"PATH": f"{tool_bin}:/usr/bin:/bin"}
    return subprocess.run([_BASH, "-c", script], capture_output=True, text=True, env=env, check=False)


def test_a_venv_off_its_constraints_refuses_init_once_and_names_the_package(tmp_path: Path) -> None:
    result = _run_guard(tmp_path, "pytest==0.0.1\n")

    assert result.returncode == 1
    assert "INIT_COMPLETE" not in result.stdout
    assert result.stderr.count("FATAL init refusing to start") == 1
    assert f"pytest {version('pytest')} installed, 0.0.1 constrained" in result.stderr
    assert "restore index access and re-run init" in result.stderr


def test_a_venv_on_its_constraints_completes_init(tmp_path: Path) -> None:
    result = _run_guard(tmp_path, f"pytest=={version('pytest')}\n")

    assert result.returncode == 0, result.stderr
    assert "INIT_COMPLETE" in result.stdout


def test_init_checks_skew_before_starting_roles() -> None:
    body = ENTRYPOINT.read_text(encoding="utf-8")
    init = re.search(r"^init\)\n(.*?)^    ;;$", body, re.MULTILINE | re.DOTALL)
    assert init is not None
    assert f"{_GUARD} init" in init.group(1)
    assert init.group(1).index(f"{_GUARD} init") < init.group(1).index("t3 teatree db migrate")
    compose = (_TEATREE / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
    assert 'restart: "no"' in compose
    assert compose.count("condition: service_completed_successfully") >= 3
