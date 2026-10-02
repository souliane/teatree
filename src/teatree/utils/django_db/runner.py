"""The manage.py interpreter-prefix chokepoint (#1973, #1976).

The SOLE site that emits a ``manage.py`` interpreter prefix, so the
pipenv-vs-uv dependency-manager detection lives in one place; a hand-rolled
second prefix silently diverges (pinned by ``test_runner_prefix_chokepoint``).
"""

import sys
from pathlib import Path

from teatree.utils.run import TimeoutExpired, run_allowed_to_fail
from teatree.utils.venv_artifacts import foreign_venv_interpreter


def _is_pipenv_repo(repo: Path) -> bool:
    """True iff *repo* is managed by pipenv rather than uv.

    A repo is pipenv-managed when it carries a ``Pipfile`` and has no usable
    ``uv.lock`` — either no lock at all, or a stub lock with no resolved
    packages (only ``version``/``revision``/``requires-python``). Running
    ``uv --directory <repo> run`` against such a stub builds a bare venv with
    none of the repo's deps, so ``import django`` fails (souliane/teatree#1973).
    """
    if not (repo / "Pipfile").is_file():
        return False
    lock = repo / "uv.lock"
    if not lock.is_file():
        return True
    try:
        return "[[package]]" not in lock.read_text(encoding="utf-8")
    except OSError:
        return True


def runner_prefix(repo: Path) -> list[str]:
    """Build the interpreter prefix that runs ``python`` from *repo*'s environment.

    The SOLE site that emits a ``manage.py`` interpreter prefix (migrate +
    overlay ``managepy`` / ``db_worker`` route here) so the pipenv-vs-uv
    detection lives in one place; a hand-rolled second prefix silently diverges
    (souliane/teatree#1976, #1973; pinned by ``test_runner_prefix_chokepoint``).
    Pipenv repos (:func:`_is_pipenv_repo`) use ``pipenv run`` with
    ``PIPENV_PIPFILE`` pinned (cwd-independent); else ``uv --directory <repo> run``.
    """
    if _is_pipenv_repo(repo):
        return ["env", f"PIPENV_PIPFILE={repo / 'Pipfile'}", "pipenv", "run", "python"]
    return ["uv", "--directory", str(repo), "run", "python"]


def project_env_is_drivable(repo: Path) -> bool:
    """Whether *repo*'s virtualenv belongs to the interpreter platform running now.

    ``uv run`` — what :func:`runner_prefix` emits — REMOVES and recreates a ``.venv``
    whose interpreter it cannot use. That is correct for a project whose environment
    uv owns, and destructive across a container boundary: ``deploy/t3`` bind-mounts
    the operator's working tree into the container, so the tree the container would
    drive carries the HOST's ``.venv``. One containerized ``t3 <overlay> tasks …``
    would delete the environment the host is actively working in (and the host's next
    ``uv run`` would delete the replacement) — a working tree destroyed by a read-only
    status command.

    A repo with no ``.venv`` is drivable: uv creates one, destroying nothing.

    ABSENCE of the recorded interpreter is not a sufficient test, and stopped being
    one the moment the two venues were given ONE interpreter root at ONE address
    (#4642): under a true identity mount the container's interpreter directory is
    PRESENT on the host and vice-versa, so an existence check calls every foreign
    environment drivable and hands it straight to the ``uv run`` that deletes it.
    :func:`~teatree.utils.venv_artifacts.foreign_venv_interpreter` carries both
    signals — the absent home, and the uv platform tag naming an OS this is not —
    and is the single place either is judged.
    """
    return foreign_venv_interpreter(repo / ".venv", platform=sys.platform) is None


_IMPORT_PROBE_TIMEOUT_SECONDS = 15


def project_env_import_error(repo: Path, module: str = "django.apps") -> str | None:
    """Why *repo*'s own ``.venv`` interpreter cannot import *module*, or ``None`` when it can.

    ``None`` too when there is no interpreter to ask (``uv run`` creates the venv) or the
    probe itself cannot finish — it only explains a failure, so it never invents one.
    """
    interpreter = repo / ".venv" / "bin" / "python"
    if not interpreter.is_file():
        return None
    try:
        probe = run_allowed_to_fail(
            [str(interpreter), "-c", f"import {module}"], expected_codes=None, timeout=_IMPORT_PROBE_TIMEOUT_SECONDS
        )
    except (OSError, TimeoutExpired):
        return None
    if probe.returncode == 0:
        return None
    lines = [line.strip() for line in probe.stderr.splitlines() if line.strip()]
    return lines[-1] if lines else f"exit {probe.returncode}"
