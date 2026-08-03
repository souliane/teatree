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

    A repo with no ``.venv`` is drivable: uv creates one, destroying nothing. A repo
    whose ``.venv`` names an interpreter that exists here is ours. A ``.venv`` whose
    recorded interpreter home is absent belongs to the other side of the boundary, and
    the caller must reach the code some other way rather than have uv clear it out.
    """
    try:
        lines = (repo / ".venv" / "pyvenv.cfg").read_text(encoding="utf-8").splitlines()
    except OSError:
        return True
    for line in lines:
        key, _, value = line.partition("=")
        if key.strip() == "home":
            return Path(value.strip()).is_dir()
    return True
