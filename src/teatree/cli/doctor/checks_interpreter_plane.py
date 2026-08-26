"""``_check_interpreter_plane`` — the venue's uv interpreter root, made loud (#4642).

A ``.venv/pyvenv.cfg`` records the ABSOLUTE path of the interpreter it was built
against. The worktree tree is bind-mounted path-identically into the container,
so both venues read the SAME environment — and while their interpreter roots
differed, each judged the other's environment invalid and DELETED it. The rebuild
is silent, costs about a gigabyte, and repeats on every flip.

The deploy fix removes the trigger by giving both venues one root at one absolute
path. This probe covers the residue: a root that cannot supply the project's
Python, and a checkout whose recorded ``home`` names a root this venue does not
have. Both are reported, never repaired — deleting or rebuilding an environment
is the very cost this issue is about.
"""

import re
from pathlib import Path

import typer


def _required_python_floor() -> tuple[int, int]:
    """The lowest Python teatree declares it runs on, as ``(major, minor)``."""
    import sys  # noqa: PLC0415 — deferred: keeps CLI startup light
    from importlib.metadata import PackageNotFoundError, metadata  # noqa: PLC0415 — deferred: keeps CLI startup light

    try:
        declared = metadata("teatree").get("Requires-Python") or ""
    except PackageNotFoundError:
        declared = ""
    match = re.search(r"(\d+)\.(\d+)", declared)
    return (int(match[1]), int(match[2])) if match else sys.version_info[:2]


def _interpreter_root() -> Path:
    """This venue's effective uv interpreter root — configured, else uv's default."""
    import os  # noqa: PLC0415 — deferred: keeps CLI startup light

    configured = os.environ.get("UV_PYTHON_INSTALL_DIR", "").strip()
    return Path(configured) if configured else Path.home() / ".local" / "share" / "uv" / "python"


def _installed_versions(root: Path) -> list[tuple[int, int]]:
    versions = []
    for entry in root.glob("cpython-*"):
        match = re.match(r"cpython-(\d+)\.(\d+)", entry.name)
        if match:
            versions.append((int(match[1]), int(match[2])))
    return versions


def _recorded_interpreter_home(pyvenv_cfg: Path) -> Path | None:
    for line in pyvenv_cfg.read_text(encoding="utf-8", errors="replace").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "home":
            return Path(value.strip())
    return None


def _checkout_pyvenv_cfgs(worktree_root: Path) -> list[Path]:
    """Every worktree checkout's ``.venv/pyvenv.cfg``, at the two depths they sit at.

    Bounded globs rather than a walk: a checkout carries whole dependency trees,
    and the answer never lies deeper than ``<ticket>/<repo>/.venv``.
    """
    return sorted({*worktree_root.glob("*/.venv/pyvenv.cfg"), *worktree_root.glob("*/*/.venv/pyvenv.cfg")})


def _root_supply_failure(root: Path, floor: tuple[int, int]) -> str | None:
    if any(version >= floor for version in _installed_versions(root)):
        return None
    return (
        f"FAIL  This venue's uv interpreter root {root} holds no cpython >= "
        f"{floor[0]}.{floor[1]}. Every `uv run` here will build against an interpreter "
        f"the other venue cannot resolve. Run `uv python install {floor[0]}.{floor[1]}`."
    )


def _venue_mismatch_failures(worktree_root: Path, root: Path) -> list[str]:
    failures = []
    for pyvenv_cfg in _checkout_pyvenv_cfgs(worktree_root):
        recorded = _recorded_interpreter_home(pyvenv_cfg)
        if recorded is None or recorded.exists():
            continue
        failures.append(
            f"FAIL  {pyvenv_cfg.parent.parent} records interpreter home {recorded}, which does not "
            f"exist in this venue (root {root}). Running uv there DELETES and rebuilds the "
            "environment — about a gigabyte, and again on the next flip back."
        )
    return failures


def _check_interpreter_plane() -> bool:
    """FAIL when this venue cannot supply, or cannot resolve, a checkout's interpreter.

    Crash-proof like its siblings: any resolution error degrades to a WARN so a
    doctor run never aborts here.
    """
    from teatree import config  # noqa: PLC0415 — deferred: keeps CLI startup light

    try:
        root = _interpreter_root()
        failures = [failure for failure in (_root_supply_failure(root, _required_python_floor()),) if failure]
        failures.extend(_venue_mismatch_failures(config.worktree_root(), root))
    except Exception as exc:  # noqa: BLE001 — a doctor check must never crash the run
        typer.echo(f"WARN  Interpreter-plane check crashed: {exc.__class__.__name__}: {exc}")
        return True
    for failure in failures:
        typer.echo(failure)
    return not failures
