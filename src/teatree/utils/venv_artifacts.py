"""Detect wrong-toolchain and wrong-HOST in-project virtualenv artifacts.

A clone managed by pipenv (it carries a ``Pipfile``) that also holds an
in-project ``.venv`` built by uv with nothing installed is a wrong-toolchain
artifact: it shadows pipenv's managed venvs and poisons both ``uv run`` and
``pipenv run`` (souliane/teatree#2005). The pipenv-vs-uv runner selection lives
in :mod:`teatree.utils.django_db`; this module owns the orthogonal concern of
spotting the residual empty uv venv so provision/doctor can clean it.

The wrong-HOST case is the same shape one boundary over: a ``.venv`` inside a
bind mount, built by whichever side ran ``uv`` last, records an interpreter the
OTHER side cannot run. uv never repairs such a venv in place — it REMOVES and
recreates it — and on a bind mount the removal can fail half-done
(``Directory not empty``), leaving a truncated install that still imports and
reds unrelated gates. :func:`foreign_venv_interpreter` names that repoint so it
is stated rather than discovered through the wreckage.
"""

from pathlib import Path

#: uv names a managed interpreter ``cpython-<version>-<os>-<arch>-<abi>``. Mapping each
#: OS tag to the ``sys.platform`` that can run it lets a venv whose interpreter dir still
#: EXISTS here — a path both sides of a shared mount can see — still be judged foreign.
_UV_PLATFORM_TAGS: dict[str, str] = {"linux": "linux", "macos": "darwin", "windows": "win32"}

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


def foreign_venv_interpreter(venv: Path, *, platform: str) -> str | None:
    """Why *venv*'s recorded interpreter cannot serve *platform*, or ``None`` when it can.

    Two independent signals, because either alone leaves a real repoint unseen: the
    ``home`` directory being ABSENT here (the container-built venv on a bind mount, which
    uv itself refuses), and — when it is present, as on a shared mount — its uv platform
    tag naming an OS this host is not. A venv with no ``pyvenv.cfg``, an unreadable one,
    or a ``home`` carrying no uv platform tag (a system interpreter) is not judged.
    """
    home = _pyvenv_home(venv)
    if home is None:
        return None
    if not home.is_dir():
        return f"pyvenv.cfg home={home} does not exist on this host"
    tag = next((t for t in _UV_PLATFORM_TAGS if f"-{t}-" in home.as_posix()), None)
    if tag is not None and _UV_PLATFORM_TAGS[tag] != platform:
        return f"pyvenv.cfg home={home} is a {tag} interpreter; this host is {platform}"
    return None


def _pyvenv_home(venv: Path) -> Path | None:
    """The ``home`` directory *venv*'s ``pyvenv.cfg`` records, or ``None`` if unreadable."""
    try:
        lines = (venv / "pyvenv.cfg").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and key.strip() == "home":
            return Path(value.strip())
    return None


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
