from pathlib import Path


def find_project_root() -> Path | None:
    """Walk up from this package looking for the teatree project root.

    Returns the first ancestor containing both ``.git`` and ``pyproject.toml``,
    or ``None`` when running from a non-source install.
    """
    current = Path(__file__).resolve().parent
    while current != current.parent:
        if (current / ".git").exists() and (current / "pyproject.toml").is_file():
            return current
        current = current.parent
    return None
