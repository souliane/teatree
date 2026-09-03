"""A shell hook must never take over an environment from the other side of a boundary.

``uv run --project <dir>`` SYNCS that project's environment before running, and
when <dir> is a uv WORKSPACE MEMBER the environment it reconciles is the WORKSPACE
ROOT's ``.venv``, not the member's — a hook pointed at a vendored
``vendor/teatree`` reaches the fork ROOT's ``.venv``. uv REMOVES and recreates a
``.venv`` whose interpreter it cannot use, so a hook running inside the container
against the operator's bind-mounted working tree deleted the HOST's live
environment on every invocation. The host holds that tree open, so the removal
failed half-done (``failed to remove directory '.../.venv/lib': Directory not
empty``), leaving a truncated install that still imported.

``uv_project_run_prefix`` (scripts/hooks/lib/resolve-uv.sh) redirects uv to the
hook's OWN environment for every workspace MEMBER, and for a non-member only when
a ``.venv`` at or above the project records an interpreter absent here. Membership
alone decides for a member because the second harm needs no foreign interpreter:
a perfectly usable shared environment still gets reconciled to the MEMBER's
dependency set, silently uninstalling everything only the root declares.

The hook runs for real, copied into a throwaway repo under ``tmp_path`` so
``repo_root`` resolves there and this clone is never touched. It is driven against
a ``uv`` stub that models the two behaviours under test — resolving the project
environment to the WORKSPACE ROOT, and REMOVING one it cannot use. uv is the
unstoppable external here; nothing else is stubbed.
"""

import json
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOKS = _REPO_ROOT / "scripts" / "hooks"

_CLEAN_EXIT = 0
_BANNED_TERM_EXIT = 1

_BANNED_TERM = "acmecorp"

# Marks an environment whose interpreter this platform has. One planted WITHOUT it
# is the foreign one — present on disk, recording an interpreter that is not here,
# which is exactly the bind-mounted host environment seen from the container.
_USABLE_MARKER = "usable"

# The package inside a planted environment whose survival is the whole assertion.
_CANARY = "lib/python3.13/site-packages/django"

# Models the two uv behaviours this fix turns on:
#   * ``--project <member>`` reconciles the WORKSPACE ROOT's environment, found by
#     walking up to the nearest ``[tool.uv.workspace]``;
#   * an environment uv cannot use is REMOVED and recreated;
#   * ``$UV_PROJECT_ENVIRONMENT`` (relative) re-targets it, resolved against that
#     same workspace root.
# Removals and syncs are logged separately so a test can tell "uv managed this"
# from "uv destroyed this".
_UV_STUB = f"""#!/usr/bin/env bash
set -euo pipefail
if [ "${{1:-}}" = "--version" ]; then echo "uv 0.0.0-test"; exit 0; fi
shift                 # run
project=""
while [ $# -gt 0 ]; do
    case "$1" in
        --project) project="$2"; shift 2 ;;
        python) shift; break ;;
        *) shift ;;
    esac
done
root="${{project}}"
probe="${{project}}"
for _ in 1 2 3 4; do
    if [ -f "${{probe}}/pyproject.toml" ] && grep -q 'tool.uv.workspace' "${{probe}}/pyproject.toml"; then
        root="${{probe}}"
    fi
    probe="$(dirname "${{probe}}")"
done
envdir="${{root}}/${{UV_PROJECT_ENVIRONMENT:-.venv}}"
if [ -d "${{envdir}}" ] && [ ! -f "${{envdir}}/{_USABLE_MARKER}" ]; then
    echo "${{envdir}}" >>"${{UV_STUB_REMOVED}}"
    rm -rf "${{envdir}}"
fi
mkdir -p "${{envdir}}"
: >"${{envdir}}/{_USABLE_MARKER}"
echo "${{envdir}}" >>"${{UV_STUB_SYNCS}}"
exec env "PYTHONPATH=${{T3_STUB_PYTHONPATH}}" {sys.executable} "$@"
"""


def _executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _throwaway_repo(tmp_path: Path, *, vendored: bool = False) -> tuple[Path, Path]:
    """Return ``(workspace_root, hook_repo)``.

    ``vendored=True`` reproduces the layout the bug needs: the hook lives in a
    workspace MEMBER while the environment uv reconciles sits at the root above it.
    """
    root = tmp_path / "root"
    hook_repo = root / "vendor" / "teatree" if vendored else root
    (hook_repo / "scripts" / "hooks" / "lib").mkdir(parents=True)
    for name in ("check-banned-terms.sh", "lib/resolve-uv.sh"):
        shutil.copy2(_HOOKS / name, hook_repo / "scripts" / "hooks" / name)
    (hook_repo / "scripts" / "hooks" / "check-banned-terms.sh").chmod(0o755)
    root.mkdir(parents=True, exist_ok=True)
    root.joinpath("pyproject.toml").write_text(
        '[project]\nname = "root"\n[tool.uv.workspace]\nmembers = ["vendor/teatree"]\n', encoding="utf-8"
    )
    if vendored:
        hook_repo.joinpath("pyproject.toml").write_text('[project]\nname = "member"\n', encoding="utf-8")
    return root, hook_repo


def _plant_env(root: Path, name: str, *, usable: bool) -> Path:
    """Plant an environment at *root* carrying a package whose survival is the assertion.

    ``usable`` decides whether its recorded interpreter exists HERE — the single
    signal that separates "ours" from the bind-mounted host's.
    """
    envdir = root / name
    (envdir / _CANARY).mkdir(parents=True, exist_ok=True)
    home = Path(sys.executable).parent if usable else Path("/nonexistent/other-platform/bin")
    envdir.joinpath("pyvenv.cfg").write_text(f"home = {home}\nversion_info = 3.13.12\n", encoding="utf-8")
    if usable:
        (envdir / _USABLE_MARKER).touch()
    return envdir


def _seed_db(tmp_path: Path) -> Path:
    db = tmp_path / "config.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS teatree_config_setting ("
        "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'banned_terms', ?)",
        (json.dumps([_BANNED_TERM]),),
    )
    conn.commit()
    conn.close()
    return db


def _env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {
        "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
        "HOME": str(home),
        "PYENV_ROOT": str(tmp_path / "pyenv"),
        "ASDF_DATA_DIR": str(tmp_path / "asdf"),
        "T3_CONFIG_DB": str(_seed_db(tmp_path)),
        "T3_STUB_PYTHONPATH": str(_REPO_ROOT / "src"),
        "UV_STUB_SYNCS": str(tmp_path / "syncs.log"),
        "UV_STUB_REMOVED": str(tmp_path / "removed.log"),
    }


def _run(tmp_path: Path, hook_repo: Path, sample_text: str) -> subprocess.CompletedProcess[str]:
    _executable(tmp_path / "bin" / "uv", _UV_STUB)
    sample = tmp_path / "sample.txt"
    sample.write_text(sample_text + "\n", encoding="utf-8")
    return subprocess.run(
        [str(hook_repo / "scripts" / "hooks" / "check-banned-terms.sh"), str(sample)],
        capture_output=True,
        text=True,
        check=False,
        env=_env(tmp_path),
        cwd=str(tmp_path),
    )


def _log(tmp_path: Path, name: str) -> list[str]:
    log = tmp_path / name
    return log.read_text(encoding="utf-8").split() if log.exists() else []


@pytest.mark.integration
class TestTheStubCanDetectTheDestruction:
    """The control. Without it a surviving ``.venv`` proves only a broken harness."""

    def test_the_pre_fix_form_wipes_the_workspace_root_env(self, tmp_path: Path) -> None:
        root, hook_repo = _throwaway_repo(tmp_path, vendored=True)
        canary = _plant_env(root, ".venv", usable=False) / _CANARY
        uv = _executable(tmp_path / "bin" / "uv", _UV_STUB)

        # Exactly what the hook used to exec: a plain `uv run --project <member>`.
        subprocess.run(
            [str(uv), "run", "--project", str(hook_repo), "python", "-c", "pass"],
            capture_output=True,
            check=False,
            env=_env(tmp_path),
        )

        assert not canary.exists(), "the stub must model uv REMOVING an environment it cannot use"
        assert _log(tmp_path, "removed.log") == [str(root / ".venv")]


@pytest.mark.integration
class TestAForeignEnvAboveTheHookSurvives:
    """The bind-mounted host ``.venv`` the containerized hook must not touch."""

    def test_the_workspace_root_env_is_left_exactly_as_it_stands(self, tmp_path: Path) -> None:
        root, hook_repo = _throwaway_repo(tmp_path, vendored=True)
        canary = _plant_env(root, ".venv", usable=False) / _CANARY

        result = _run(tmp_path, hook_repo, "ship the docs refresh next week")

        assert canary.exists(), (
            f"the hook destroyed the environment above it\nstdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert _log(tmp_path, "removed.log") == [], f"uv removed: {_log(tmp_path, 'removed.log')}"
        assert result.returncode == _CLEAN_EXIT, f"stdout={result.stdout!r} stderr={result.stderr!r}"

    def test_only_the_hooks_own_environment_is_reconciled(self, tmp_path: Path) -> None:
        root, hook_repo = _throwaway_repo(tmp_path, vendored=True)
        _plant_env(root, ".venv", usable=False)

        _run(tmp_path, hook_repo, "ship the docs refresh next week")

        assert _log(tmp_path, "syncs.log") == [str(root / ".venv-hook")], (
            f"a sync targeted something other than the hook's own env: {_log(tmp_path, 'syncs.log')}"
        )

    def test_the_gate_still_catches_a_planted_term(self, tmp_path: Path) -> None:
        # Anti-vacuity: non-destructive must not mean quiet.
        root, hook_repo = _throwaway_repo(tmp_path, vendored=True)
        _plant_env(root, ".venv", usable=False)

        result = _run(tmp_path, hook_repo, f"we ship to {_BANNED_TERM} next week")

        assert result.returncode == _BANNED_TERM_EXIT, f"stderr={result.stderr!r}"
        assert "BANNED TERM in" in result.stdout


@pytest.mark.integration
class TestAUsableRootEnvIsStillNotTheMembersToReconcile:
    """Membership alone decides: the interpreter being usable does not make the env ours."""

    def test_a_usable_root_env_keeps_the_packages_only_the_root_declares(self, tmp_path: Path) -> None:
        root, hook_repo = _throwaway_repo(tmp_path, vendored=True)
        canary = _plant_env(root, ".venv", usable=True) / _CANARY

        result = _run(tmp_path, hook_repo, f"we ship to {_BANNED_TERM} next week")

        assert result.returncode == _BANNED_TERM_EXIT, f"stderr={result.stderr!r}"
        assert _log(tmp_path, "syncs.log") == [str(root / ".venv-hook")]
        assert canary.exists(), "a member reconciling the root env uninstalls what only the root declares"


@pytest.mark.integration
class TestAColdCloneStillScans:
    """Non-destructive must not be traded for a broken first run."""

    def test_no_environment_at_all_still_scans_and_builds_the_hooks_own(self, tmp_path: Path) -> None:
        root, hook_repo = _throwaway_repo(tmp_path, vendored=True)
        assert not (root / ".venv").exists()

        result = _run(tmp_path, hook_repo, f"we ship to {_BANNED_TERM} next week")

        assert result.returncode == _BANNED_TERM_EXIT, (
            f"a cold clone must still scan, got {result.returncode}\nstdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "BANNED TERM in" in result.stdout
        assert _log(tmp_path, "syncs.log") == [str(root / ".venv-hook")], "a member builds its own, never the root's"
        assert not (root / ".venv").exists(), "the hook never creates the workspace root's environment"

    def test_a_cold_clone_passes_clean_text(self, tmp_path: Path) -> None:
        _, hook_repo = _throwaway_repo(tmp_path, vendored=True)
        result = _run(tmp_path, hook_repo, "nothing interesting here")
        assert result.returncode == _CLEAN_EXIT, f"stderr={result.stderr!r}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
