"""Editable dev-source override helpers for ``t3 doctor``.

Under ``contribute=true`` the doctor re-points teatree/overlay installs at a
local checkout by patching ``[tool.uv.sources]`` in the host project's
``pyproject.toml`` and recording the override in a gitignored
``.t3-dev-sources`` marker. These helpers own that concern; ``app.py``
re-exports them so ``teatree.cli.doctor._patch_uv_source`` (and the
``app._find_host_project_root`` patch path used by tests) stay intact.
"""

import os
import re
from pathlib import Path

# Files the editable-source override mutates and must keep out of the commit
# path: ``pyproject.toml`` carries the local-path source, and ``uv sync``
# rewrites ``uv.lock`` to record it.  Both are hidden from git via
# ``--assume-unchanged`` for the duration of the override and restored together.
_DEV_HIDDEN_FILES = ("pyproject.toml", "uv.lock")


def _find_host_project_root() -> Path | None:
    """Walk up from cwd to find the host project (directory with manage.py + pyproject.toml)."""
    for directory in [Path.cwd(), *Path.cwd().parents]:
        if (directory / "manage.py").is_file() and (directory / "pyproject.toml").is_file():
            return directory
    return None


def _find_teatree_pyproject_from_cwd() -> Path | None:
    """Return the teatree repo rooted at cwd, if any.

    Walks up from cwd looking for a ``pyproject.toml`` whose ``[project].name`` is
    ``teatree``.  Lets dogfood worktrees override ``T3_REPO`` so that running
    ``t3`` from a worktree reinstalls editable from the worktree, not the main clone.
    """
    for directory in [Path.cwd(), *Path.cwd().parents]:
        pyproject = directory / "pyproject.toml"
        if not pyproject.is_file():
            continue
        try:
            if re.search(r'^\s*name\s*=\s*"teatree"', pyproject.read_text(), re.MULTILINE):
                return directory
        except OSError:
            pass
        return None
    return None


def _patch_uv_source(pyproject: Path, package: str, repo_path: Path) -> bool:
    """Rewrite the ``[tool.uv.sources]`` entry for *package* to a local editable path."""
    text = pyproject.read_text(encoding="utf-8")
    # Match: package = { git = "...", branch = "..." } or package = { ... }
    pattern = rf"^({re.escape(package)}\s*=\s*)\{{[^}}]+\}}"
    relative = os.path.relpath(repo_path, pyproject.parent)
    replacement = rf'\g<1>{{ path = "{relative}", editable = true }}'
    new_text, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count == 0:
        return False
    pyproject.write_text(new_text, encoding="utf-8")
    return True


def _write_dev_sources_marker(marker: Path, package: str, repo_path: Path) -> None:
    """Append or update a line in the ``.t3-dev-sources`` marker file."""
    lines: list[str] = []
    if marker.is_file():
        lines = [ln for ln in marker.read_text(encoding="utf-8").splitlines() if not ln.startswith(f"{package}=")]
    lines.append(f"{package}={repo_path}")
    marker.write_text("\n".join(lines) + "\n", encoding="utf-8")


class SourceEntryChangedError(ValueError):
    """The editable source entry was changed after the doctor wrote it."""

    def __init__(self, tracked: str) -> None:
        super().__init__(f"{tracked} source changed after editable repair")


def _package_block(text: str, package: str) -> tuple[int, int, str] | None:
    starts = [match.start() for match in re.finditer(r"(?m)^\[\[package\]\]$", text)]
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(text)
        block = text[start:end]
        if re.search(rf'(?m)^name = "{re.escape(package)}"$', block):
            return start, end, block
    return None


def _restore_lock_source_entry(current: str, original: str, package: str) -> str:
    old_block = _package_block(original, package)
    new_block = _package_block(current, package)
    if new_block is None:
        return current
    old_source = re.search(r"(?m)^source = .+$", old_block[2]) if old_block else None
    new_source = re.search(r"(?m)^source = .+$", new_block[2])
    if new_source is None:
        return current
    if "editable =" not in new_source.group() and "path =" not in new_source.group():
        tracked = "uv.lock"
        raise SourceEntryChangedError(tracked)
    source_end = new_source.end() + new_block[2][new_source.end() :].startswith("\n")
    replacement = f"{old_source.group()}\n" if old_source else ""
    changed = new_block[2][: new_source.start()] + replacement + new_block[2][source_end:]
    return current[: new_block[0]] + changed + current[new_block[1] :]


def _restore_pyproject_source_entry(current: str, original: str, package: str) -> str:
    pattern = re.compile(rf"(?m)^({re.escape(package)}\s*=\s*)\{{[^\n}}]+\}}")
    old_entry = pattern.search(original)
    new_entry = pattern.search(current)
    if new_entry is None:
        return current
    if "editable = true" not in new_entry.group() and "path =" not in new_entry.group():
        tracked = "pyproject.toml"
        raise SourceEntryChangedError(tracked)
    if old_entry is not None:
        return current[: new_entry.start()] + old_entry.group() + current[new_entry.end() :]
    entry_end = new_entry.end() + current[new_entry.end() :].startswith("\n")
    restored = current[: new_entry.start()] + current[entry_end:]
    return re.sub(r"(?m)^\[tool\.uv\.sources\]\n(?=\[|\Z)", "", restored)


def _restore_source_entry(current: str, original: str, package: str, *, lockfile: bool) -> str:
    """Restore one package's source line while retaining every unrelated edit."""
    if lockfile:
        return _restore_lock_source_entry(current, original, package)
    return _restore_pyproject_source_entry(current, original, package)
