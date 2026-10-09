from pathlib import Path

from teatree.paths import PathHelpers, resolve_main_clone


def find_project_root() -> Path | None:
    """Walk up from this package looking for the teatree project root.

    Returns the first ancestor containing both ``.git`` and ``pyproject.toml``,
    or ``None`` when running from a non-source install. When that ancestor is a
    git *worktree* (``.git`` is a pointer file), resolves back to the primary
    clone it points at — a worktree-anchored import must not bind skills or the
    editable repo to the worktree, which silently isolates the control DB
    (#1507).
    """
    current = Path(__file__).resolve().parent
    while current != current.parent:
        if (current / "pyproject.toml").is_file():
            if PathHelpers.is_baked_generation_root(current):
                return current
            if (current / ".git").exists():
                return resolve_main_clone(current) or current
        current = current.parent
    return None
