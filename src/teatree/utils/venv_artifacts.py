"""Detect wrong-toolchain in-project virtualenv artifacts.

A clone managed by pipenv (it carries a ``Pipfile``) that also holds an
in-project ``.venv`` built by uv with nothing installed is a wrong-toolchain
artifact: it shadows pipenv's managed venvs and poisons both ``uv run`` and
``pipenv run`` (souliane/teatree#2005). The pipenv-vs-uv runner selection lives
in :mod:`teatree.utils.django_db`; this module owns the orthogonal concern of
spotting the residual empty uv venv so provision/doctor can clean it.
"""

from pathlib import Path

#: Files uv/virtualenv drop into a freshly-built venv before any dependency is
#: installed. A site-packages holding only these (no ``*.dist-info`` /
#: ``*.egg-info`` and no real package) is an empty venv with nothing installed.
_BOOTSTRAP_VENV_FILES = frozenset({"_virtualenv.pth", "_virtualenv.py", "__pycache__", "pip", "pip.dist-info"})


def find_stale_uv_venv(repo: Path) -> Path | None:
    """Return *repo*'s in-project ``.venv`` iff it is a stale uv-built empty one.

    A clone carrying a ``Pipfile`` (pipenv-managed) that also has an in-project
    ``.venv`` whose ``pyvenv.cfg`` was written by uv (a ``uv = ...`` line) and
    which has no dependency installed is the wrong-toolchain artifact described
    in the module docstring. Returns the offending ``.venv`` path so the caller
    can warn or remove it; ``None`` when the repo is not pipenv-managed, has no
    ``.venv``, the venv was not uv-built, or the venv actually has packages
    installed.
    """
    if not (repo / "Pipfile").is_file():
        return None
    venv = repo / ".venv"
    cfg = venv / "pyvenv.cfg"
    if not cfg.is_file():
        return None
    try:
        if not any(line.lstrip().startswith("uv ") for line in cfg.read_text(encoding="utf-8").splitlines()):
            return None
    except OSError:
        return None
    if _venv_has_packages(venv):
        return None
    return venv


def _venv_has_packages(venv: Path) -> bool:
    """True iff *venv*'s site-packages holds an installed distribution.

    Walks every ``site-packages`` under the venv (``lib/python*/site-packages``
    on POSIX, ``Lib/site-packages`` on Windows) and reports whether any entry
    beyond the bootstrap files (:data:`_BOOTSTRAP_VENV_FILES`) is present — a
    real package directory, module, or ``*.dist-info`` / ``*.egg-info``.
    """
    return any(
        entry.name not in _BOOTSTRAP_VENV_FILES
        for site in venv.glob("**/site-packages")
        if site.is_dir()
        for entry in site.iterdir()
    )
