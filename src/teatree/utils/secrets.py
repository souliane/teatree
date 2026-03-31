"""Secret retrieval via the ``pass`` password store."""

import subprocess


def read_pass(key: str) -> str:
    """Read a secret from the ``pass`` password store.

    Returns the first line of the stored value, or an empty string
    if the key is not found or ``pass`` is not installed.
    """
    try:
        result = subprocess.run(
            ["pass", "show", key],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip().splitlines()[0]
    except (subprocess.CalledProcessError, FileNotFoundError, IndexError):
        return ""
