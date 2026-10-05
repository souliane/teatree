"""A long-lived role refuses to start on a tool venv the boot constraints do not describe.

Run the way the entrypoint runs it: the checker file under an interpreter, reading a
constraints file, against whatever that interpreter has installed.
"""

import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

from teatree.utils.constraint_skew import constraint_skew

_CHECKER = Path(__file__).resolve().parents[2] / "src" / "teatree" / "utils" / "constraint_skew.py"


def _check(constraints: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_CHECKER), str(constraints)], capture_output=True, text=True, check=False
    )


def test_an_installed_version_off_its_pin_is_named(tmp_path: Path) -> None:
    constraints = tmp_path / "uv-constraints.txt"
    constraints.write_text("pytest==0.0.1\n", encoding="utf-8")

    result = _check(constraints)

    assert result.returncode == 1
    assert f"pytest {version('pytest')} installed, 0.0.1 constrained" in result.stdout


def test_a_venv_on_every_pin_passes(tmp_path: Path) -> None:
    constraints = tmp_path / "uv-constraints.txt"
    constraints.write_text(f"# header\npytest=={version('pytest')}\n    # via teatree\n", encoding="utf-8")

    result = _check(constraints)

    assert result.returncode == 0, result.stdout


def test_a_pin_for_a_package_the_venv_does_not_carry_is_not_skew() -> None:
    assert constraint_skew("some-extra-only-package==1.0\n", {}) == []


def test_names_and_markers_are_read_the_way_uv_export_writes_them() -> None:
    lines = "Typing_Extensions==4.12.2 ; python_full_version < '3.14'\nuvicorn[standard]==0.34.0\n"

    skew = constraint_skew(lines, {"typing-extensions": "4.12.2", "uvicorn": "0.33.0"})

    assert skew == ["uvicorn 0.33.0 installed, 0.34.0 constrained"]


def test_a_comment_only_constraints_file_constrains_nothing() -> None:
    assert constraint_skew("# uv export failed at boot - no constraints applied.\n", {"rq": "2.0"}) == []


def test_a_package_pinned_once_per_marker_is_satisfied_by_either_pin() -> None:
    lines = "numpy==1.26.4 ; python_full_version < '3.12'\nnumpy==2.3.1 ; python_full_version >= '3.12'\n"

    assert constraint_skew(lines, {"numpy": "2.3.1"}) == []
    assert constraint_skew(lines, {"numpy": "2.0.0"}) == ["numpy 2.0.0 installed, 1.26.4 or 2.3.1 constrained"]
