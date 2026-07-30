"""The manage.py interpreter-prefix chokepoint (#1973, #1976).

The SOLE site that emits a ``manage.py`` interpreter prefix, so the
pipenv-vs-uv dependency-manager detection lives in one place; a hand-rolled
second prefix silently diverges (pinned by ``test_runner_prefix_chokepoint``).
"""

from pathlib import Path


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
