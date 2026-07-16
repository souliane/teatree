"""Detect + repair a dangling teatree editable ``.pth`` (the reaped-worktree footgun).

The global ``uv tool install --editable`` of teatree puts an editable ``.pth``
file under the tool's site-packages
(``~/.local/share/uv/tools/teatree/lib/python*/site-packages/teatree.pth``) whose
single line is the teatree ``src`` directory the launcher ``~/.local/bin/t3``
imports ``t3_bootstrap`` through. A sub-agent that repoints that ``.pth`` at its
OWN worktree — then has that worktree reaped by ``clean-all`` — leaves the line
pointing at a non-existent dir. Every ``t3`` invocation then dies machine-wide
with ``ModuleNotFoundError: No module named 't3_bootstrap'``, blocking all
workspace/DB/test ops.

The same hazard lives in the uv tool *receipt*
(``~/.local/share/uv/tools/teatree/uv-receipt.toml``), whose
``[tool].requirements[].editable`` records the source clone — a stale value
re-breaks the ``.pth`` on the next ``t3 update`` / reinstall.

This module is the Django-free detection + safe-repair primitive a ``t3 doctor``
check consumes. It depends only on the stdlib so it stays usable even when the
running env is itself partially broken.
"""

import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

_TOOL_NAME = "teatree"
_PTH_NAME = f"{_TOOL_NAME}.pth"
_RECEIPT_NAME = "uv-receipt.toml"


def uv_tool_dir() -> Path:
    """Return uv's tool dir (``$UV_TOOL_DIR`` or the default), not necessarily existing."""
    env_dir = os.environ.get("UV_TOOL_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    return Path.home() / ".local" / "share" / "uv" / "tools"


def teatree_pth_path() -> Path | None:
    """Return the teatree editable ``.pth`` under the uv tool install, if present.

    Resolves ``<tool-dir>/teatree/lib/python*/site-packages/teatree.pth`` —
    the editable link the global ``t3`` launcher imports through. Returns
    ``None`` when the tool install or the ``.pth`` is absent (a non-uv-tool
    install, or teatree not installed as a tool).
    """
    site_root = uv_tool_dir() / _TOOL_NAME / "lib"
    if not site_root.is_dir():
        return None
    for py_dir in sorted(site_root.glob("python*")):
        candidate = py_dir / "site-packages" / _PTH_NAME
        if candidate.is_file():
            return candidate
    return None


def pth_source_dirs(pth: Path) -> list[Path]:
    """Return the directory paths a ``.pth`` file adds to ``sys.path``.

    A ``.pth`` line that is blank, a comment (``#``), or an ``import`` directive
    is not a path entry and is skipped, matching the ``site`` module's own rules.
    """
    dirs: list[Path] = []
    try:
        lines = pth.read_text(encoding="utf-8").splitlines()
    except OSError:
        return dirs
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith(("#", "import ", "import\t")):
            continue
        dirs.append(Path(line))
    return dirs


def receipt_editable_source() -> Path | None:
    """Return the editable source clone recorded in uv's teatree receipt, or ``None``.

    Reads ``<tool-dir>/teatree/uv-receipt.toml``'s
    ``[tool].requirements[].editable`` for the ``teatree`` requirement. Returns
    ``None`` when the receipt is absent, unparsable, or records a non-editable
    install.
    """
    receipt = uv_tool_dir() / _TOOL_NAME / _RECEIPT_NAME
    if not receipt.is_file():
        return None
    try:
        data = tomllib.loads(receipt.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return None
    for req in data.get("tool", {}).get("requirements", []):
        if req.get("name") == _TOOL_NAME and req.get("editable"):
            return Path(req["editable"])
    return None


@dataclass(frozen=True, slots=True)
class DanglingEditable:
    """A teatree editable link that points at a directory which no longer exists.

    ``pth`` / ``pth_dangling_dir`` describe the ``.pth`` link; ``receipt_source``
    is set only when the uv receipt's editable clone is itself gone. Either
    surface being dangling makes the install fragile — the ``.pth`` one breaks
    ``t3`` immediately, the receipt one re-breaks it on the next reinstall.
    """

    pth: Path | None
    pth_dangling_dir: Path | None
    receipt_source: Path | None

    @property
    def is_dangling(self) -> bool:
        return self.pth_dangling_dir is not None or self.receipt_source is not None


def detect_dangling_editable() -> DanglingEditable:
    """Inspect the teatree editable ``.pth`` + uv receipt for a non-existent target.

    Returns a :class:`DanglingEditable`; ``is_dangling`` is ``True`` when the
    ``.pth`` adds a path whose directory is gone, or the receipt records an
    editable clone that no longer exists. A healthy install yields an instance
    whose ``is_dangling`` is ``False``.
    """
    pth = teatree_pth_path()
    pth_dangling_dir: Path | None = None
    if pth is not None:
        for source in pth_source_dirs(pth):
            if not source.is_dir():
                pth_dangling_dir = source
                break
    receipt_src = receipt_editable_source()
    receipt_dangling = receipt_src if receipt_src is not None and not receipt_src.is_dir() else None
    return DanglingEditable(pth=pth, pth_dangling_dir=pth_dangling_dir, receipt_source=receipt_dangling)


def canonical_src_dir() -> Path | None:
    """Return ``$T3_REPO/src`` when it exists, the safe re-anchor target.

    ``$T3_REPO`` is the canonical clone. Returns ``None`` when the env var is
    unset or its ``src`` directory is absent — there is then no safe target to
    repair to, so the caller reports the dangling link without auto-repairing.
    """
    repo = os.environ.get("T3_REPO", "")
    if not repo:
        return None
    src = Path(repo).expanduser() / "src"
    return src if src.is_dir() else None


def expected_checkout() -> Path | None:
    """Return the checkout the active ``t3`` editable install SHOULD point at (#3231).

    The canonical clone is ``$T3_REPO`` (a repo root — the same value
    ``uv tool install --editable .`` records in the receipt's
    ``requirements[].editable``). Returns the resolved path when ``$T3_REPO`` is
    set and exists, else ``None`` — with no known expected checkout there is
    nothing to compare the receipt against, so the shim-receipt check skips
    rather than guess.
    """
    repo = os.environ.get("T3_REPO", "")
    if not repo:
        return None
    path = Path(repo).expanduser()
    return path.resolve() if path.is_dir() else None


def repair_receipt_to_checkout(checkout: Path) -> bool:
    """Re-point the ``t3`` editable uv-tool install at *checkout*; return success (#3231).

    Runs ``uv tool install --editable <checkout> --force`` — the supported way
    to re-anchor a relocated or same-name-hijacked editable install at its
    correct source, rewriting the shim, ``.pth``, and receipt in one step. Fails
    safe to ``False`` when ``uv`` is absent or the install errors, so the caller
    still reports the problem rather than claiming a repair.
    """
    import shutil  # noqa: PLC0415 — deferred: keeps the stdlib-only detection path light

    from teatree.utils.run import (  # noqa: PLC0415 — deferred: only the repair path shells out
        CommandFailedError,
        TimeoutExpired,
        run_allowed_to_fail,
    )

    uv = shutil.which("uv")
    if uv is None:
        return False
    try:
        result = run_allowed_to_fail(
            [uv, "tool", "install", "--editable", str(checkout), "--force"],
            expected_codes=None,
            timeout=300,
        )
    except (OSError, TimeoutExpired, CommandFailedError):
        return False
    return result.returncode == 0


def _is_path_entry(line: str) -> bool:
    """Whether a ``.pth`` line is a ``sys.path`` entry (not blank/comment/import).

    Mirrors the ``site`` module's own rules — the same predicate
    :func:`pth_source_dirs` filters on.
    """
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith(("#", "import ", "import\t"))


def repair_pth_to_canonical(pth: Path, canonical_src: Path) -> bool:
    """Rewrite ``pth`` to point at ``canonical_src``; return whether it changed.

    Only the path entries are rewritten — ``import`` / comment / blank lines are
    kept verbatim, in place. The first path entry becomes ``canonical_src`` and
    any further path entries are dropped (the editable install is a single
    ``src`` dir), so the relative order of preserved non-path lines is unchanged.
    Idempotent: when the ``.pth`` already names exactly ``canonical_src`` (and no
    other path entry), nothing is written and ``False`` is returned. Fails safe
    to ``False`` on any read/write error so the caller still reports the problem
    rather than claiming a repair.
    """
    target = str(canonical_src)
    if [str(d) for d in pth_source_dirs(pth)] == [target]:
        return False
    try:
        original = pth.read_text(encoding="utf-8")
    except OSError:
        return False

    rebuilt: list[str] = []
    canonical_written = False
    for raw in original.splitlines():
        if _is_path_entry(raw):
            if not canonical_written:
                rebuilt.append(target)
                canonical_written = True
            # Drop any additional path entries — collapse to the single canonical src.
        else:
            rebuilt.append(raw)
    if not canonical_written:
        rebuilt.append(target)

    try:
        pth.write_text(os.linesep.join(rebuilt) + os.linesep, encoding="utf-8")
    except OSError:
        return False
    return True


def running_from_canonical_clone() -> bool:
    """Whether the running ``t3`` already imports teatree from ``$T3_REPO/src``.

    Auto-repair of the ``.pth`` is only safe when the process running the repair
    is NOT itself resolving teatree through that ``.pth`` from a worktree (which
    would re-anchor the global install at a transient checkout — the exact #1507
    footgun). True only when the running ``teatree`` package lives under the
    canonical ``$T3_REPO/src``.
    """
    canonical = canonical_src_dir()
    if canonical is None:
        return False
    try:
        running = Path(sys.modules["teatree"].__file__ or "").resolve().parent.parent
        return running == canonical.resolve()
    except (OSError, AttributeError, KeyError):
        return False
