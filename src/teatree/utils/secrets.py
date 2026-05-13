"""Secret storage via the ``pass`` password store."""

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


def write_pass(key: str, value: str) -> bool:
    """Store *value* under *key* in the ``pass`` password store.

    Uses ``pass insert --multiline --force`` so the secret is read from
    stdin and an existing entry is overwritten silently. Returns ``True``
    on success, ``False`` if ``pass`` is not installed or the call failed.
    """
    try:
        run_checked(["pass", "insert", "--multiline", "--force", key], stdin_text=value)
    except (CommandFailedError, FileNotFoundError):
        return False
    return True


def remove_pass(key: str) -> bool:
    """Remove *key* from the ``pass`` password store.

    Uses ``pass rm --force`` so the entry is deleted without prompting.
    Returns ``True`` on success, ``False`` if the entry was absent, ``pass``
    is not installed, or the call failed.
    """
    try:
        run_checked(["pass", "rm", "--force", key])
    except (CommandFailedError, FileNotFoundError):
        return False
    return True


def pass_entry_exists(key: str) -> bool:
    """Return ``True`` when *key* resolves to a non-empty entry in ``pass``."""
    return bool(read_pass(key))
