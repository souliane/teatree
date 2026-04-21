"""Secret retrieval via the ``pass`` password store."""

from teatree.utils.run import CommandFailedError, run_checked


def read_pass(key: str) -> str:
    """Read a secret from the ``pass`` password store.

    Returns the first line of the stored value, or an empty string
    if the key is not found or ``pass`` is not installed.
    """
    try:
        result = run_checked(["pass", "show", key])
    except (CommandFailedError, FileNotFoundError):
        return ""
    lines = result.stdout.strip().splitlines()
    return lines[0] if lines else ""
